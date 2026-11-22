from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
import yaml

from boatyard.markers import declared_dockerfile_paths
from boatyard.models.image import dockerfile_stem, require_dockerfile_spelling
from boatyard.yaml import Yaml, is_list, is_mapping

PROBE_ADAPTER_ANNOTATION = "boatyard/probe-adapter"
PROBE_ADAPTERS = ("python-urllib", "bash-http")
RESTART_ANNOTATION = "boatyard/restart"
COMPOSE_RESTART_POLICIES = ("no", "always", "on-failure", "unless-stopped")
CONSUMER_INTERPOLATED_ANNOTATION = "boatyard/consumer-interpolated"
DOCKERFILE_ANNOTATION = "boatyard/dockerfile"
AUTOSTART_ANNOTATION = "boatyard/autostart"
LOCK_ANNOTATION = "boatyard/lock"
ROUTE_ANNOTATION = "boatyard/route"
ROUTE_PORT_ANNOTATION = "boatyard/port"
PUBLISH_PORT_ANNOTATION = "boatyard/publish-port"
CONTAINER_PORTS_ANNOTATION = "boatyard/container-ports"

ROUTE_PATH_PATTERN = re.compile(r"(?:/\*|/(?:[^/\s*]+(?:/[^/\s*]+)*)?(?:/\*)?)")

ROUTE_PORT_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")

ROUTE_PATH_EXPORT_SUFFIX = "ROUTE_PATH"

PUBLISH_PORT_PROTOCOLS: tuple[Literal["tcp", "udp"], ...] = ("tcp", "udp")


def _default_metadata() -> WorkloadMetadata:
    return WorkloadMetadata()


class WorkloadDocument(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    metadata: WorkloadMetadata = Field(default_factory=_default_metadata)
    containers: dict[str, WorkloadContainer] = Field(default_factory=dict)
    resources: dict[str, WorkloadResource] = Field(default_factory=dict)
    service: WorkloadService | None = None

    @classmethod
    def parse(cls, raw: Yaml, file_name: str) -> Self:
        return cls.model_validate(raw, context={"source": file_name})

    @model_validator(mode="after")
    def _require_name_matching_file_stem(self, info: ValidationInfo) -> Self:
        source = info.context.get("source") if info.context is not None else None
        if source is not None and self.metadata.name != Path(source).stem:
            raise ValueError(f"metadata.name must be {Path(source).stem!r} to match the workload file name")
        return self

    @model_validator(mode="after")
    def _validate_deployment_surface_annotations(self) -> Self:
        annotations = self.metadata.annotations

        adapter = annotations.get(PROBE_ADAPTER_ANNOTATION)
        if adapter is not None and adapter not in PROBE_ADAPTERS:
            raise ValueError(
                f"{PROBE_ADAPTER_ANNOTATION} must be one of the supported adapters {list(PROBE_ADAPTERS)}, not {adapter!r}"
            )

        route = annotations.get(ROUTE_ANNOTATION)
        if route is not None:
            resolve_route_port(parse_route_annotation(route), self.service_ports)

        listeners = self.listener_ports
        for name, container in self.containers.items():
            for probe, block in (
                ("readinessProbe", container.readiness_probe),
                ("livenessProbe", container.liveness_probe),
            ):
                if block is None or block.http_get is None:
                    continue
                http_get = block.http_get
                if not is_mapping(http_get):
                    raise ValueError(f"the container {name!r} {probe} httpGet must be a mapping")
                if "port" not in http_get:
                    raise ValueError(
                        f"the container {name!r} {probe} httpGet declares no port; name a service.ports "
                        f"entry or a {CONTAINER_PORTS_ANNOTATION} listener"
                    )
                port = http_get["port"]
                match port:
                    case str():
                        if port not in listeners:
                            raise ValueError(
                                f"the container {name!r} {probe} httpGet port {port!r} names no declared "
                                f"listener ({', '.join(sorted(listeners))})"
                            )
                    case _:
                        raise ValueError(
                            f"the container {name!r} {probe} httpGet port {port!r}: the literal-port grammar "
                            f"is removed; probe ports are listener names, so name a service.ports entry or "
                            f"a {CONTAINER_PORTS_ANNOTATION} listener"
                        )

        publish_port = annotations.get(PUBLISH_PORT_ANNOTATION)
        if publish_port is not None:
            parse_publish_port_annotation(publish_port)

        restart = annotations.get(RESTART_ANNOTATION)
        if restart is not None and restart not in COMPOSE_RESTART_POLICIES:
            raise ValueError(
                f"{RESTART_ANNOTATION} must be one of the compose restart policies "
                f"{list(COMPOSE_RESTART_POLICIES)}, not {restart!r}"
            )

        consumer_interpolated = annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
        if consumer_interpolated is not None:
            if not consumer_interpolated.strip():
                raise ValueError(
                    f"{CONSUMER_INTERPOLATED_ANNOTATION} must be a non-empty comma-separated "
                    "list of the workload's files: paths"
                )
            declared_files = {target for container in self.containers.values() for target in container.files}
            for entry in consumer_interpolated.split(","):
                if entry != entry.strip() or not entry:
                    raise ValueError(
                        f"{CONSUMER_INTERPOLATED_ANNOTATION} entry {entry!r} must be non-empty and unpadded"
                    )
                if entry not in declared_files:
                    raise ValueError(
                        f"{CONSUMER_INTERPOLATED_ANNOTATION} entry {entry!r} names no declared files: path"
                    )

        dockerfiles = _parse_source_annotation(annotations, DOCKERFILE_ANNOTATION)
        locks = _parse_source_annotation(annotations, LOCK_ANNOTATION)
        both = sorted(dockerfiles.keys() & locks.keys())
        if both:
            raise ValueError(
                f"the container {both[0]!r} is named by both {DOCKERFILE_ANNOTATION} and {LOCK_ANNOTATION}"
            )
        unsourced = sorted(set(self.containers) - dockerfiles.keys() - locks.keys())
        if unsourced:
            raise ValueError(
                f"the container {unsourced[0]!r} carries no source annotation "
                f"({DOCKERFILE_ANNOTATION} or {LOCK_ANNOTATION})"
            )
        unknown = sorted((dockerfiles.keys() | locks.keys()) - set(self.containers))
        if unknown:
            raise ValueError(f"the source annotations name no container {unknown[0]!r} of this workload")
        for dockerfile in dockerfiles.values():
            require_dockerfile_spelling(dockerfile)

        return self

    @property
    def consumer_interpolated_targets(self) -> list[str]:
        annotation = self.metadata.annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
        return annotation.split(",") if annotation is not None else []

    @property
    def service_ports(self) -> dict[str, int | WorkloadPortSpec]:
        return self.service.ports if self.service is not None else {}

    @property
    def listener_ports(self) -> dict[str, int]:
        # The names a probe port may reference: the service.ports listen ports plus the
        # deliberately-not-service listeners the container-ports annotation declares
        listeners = {name: listen_port(spec) for name, spec in self.service_ports.items()}
        annotation = self.metadata.annotations.get(CONTAINER_PORTS_ANNOTATION)
        if annotation is None:
            return listeners
        declared = parse_container_ports_annotation(annotation)
        collisions = sorted(listeners.keys() & declared.keys())
        if collisions:
            raise ValueError(
                f"{CONTAINER_PORTS_ANNOTATION} names the listener {collisions[0]!r} that service.ports already declares"
            )
        listeners.update(declared)
        return listeners

    @property
    def resolved_route(self) -> ResolvedRoute | None:
        annotation = self.metadata.annotations.get(ROUTE_ANNOTATION)
        if annotation is None:
            return None
        claim = parse_route_annotation(annotation)
        return ResolvedRoute(
            path=claim.path,
            port=resolve_route_port(claim, self.service_ports),
            strip_prefix=claim.strip_prefix,
        )

    @property
    def published_ports(self) -> list[WorkloadPublishedPort]:
        annotation = self.metadata.annotations.get(PUBLISH_PORT_ANNOTATION)
        return parse_publish_port_annotation(annotation) if annotation is not None else []

    def container_sources(self) -> dict[str, tuple[str, str]]:
        dockerfiles = _parse_source_annotation(self.metadata.annotations, DOCKERFILE_ANNOTATION)
        locks = _parse_source_annotation(self.metadata.annotations, LOCK_ANNOTATION)
        return {
            **{name: ("dockerfile", value) for name, value in dockerfiles.items()},
            **{name: ("lock", value) for name, value in locks.items()},
        }

    @property
    def dockerfile_stems(self) -> set[str]:
        return {dockerfile_stem(value) for kind, value in self.container_sources().values() if kind == "dockerfile"}

    def provisioners(self, score_dir: Path, rendering: str) -> list[tuple[str, list[Yaml]]]:
        # A provisioner file joins this workload when one of its entries names a declared resource type
        referenced = {resource.type for resource in self.resources.values()}
        rendering_dir = score_dir / rendering
        if not rendering_dir.is_dir():
            return []
        selected: list[tuple[str, list[Yaml]]] = []
        for path in sorted(rendering_dir.glob("*.yaml")):
            entries = yaml.safe_load(path.read_text(encoding="utf-8"))
            if is_list(entries) and any(is_mapping(entry) and entry.get("type") in referenced for entry in entries):
                selected.append((f"{rendering}/{path.name}", entries))
        return selected

    def declared_dockerfiles(self, score_dir: Path) -> list[str]:
        # The workload's full dockerfile demand: its container sources and its provisioners' markers
        paths = [value for kind, value in self.container_sources().values() if kind == "dockerfile"]
        for rendering in ("compose", "k8s"):
            for _name, entries in self.provisioners(score_dir, rendering):
                paths.extend(
                    path for entry in entries if is_mapping(entry) for path in declared_dockerfile_paths(entry)
                )
        return paths


def _parse_source_annotation(annotations: dict[str, str], name: str) -> dict[str, str]:
    value = annotations.get(name)
    if value is None:
        return {}
    if not value.strip():
        raise ValueError(f"{name} must be a non-empty comma-separated list of container=source pairs")
    sources: dict[str, str] = {}
    for entry in value.split(","):
        container, separator, source = entry.partition("=")
        padded = container != container.strip() or source != source.strip()
        if not separator or padded or not container or not source:
            raise ValueError(f"{name} entry {entry!r} must be an unpadded container=source pair")
        if container in sources:
            raise ValueError(f"{name} names the container {container!r} twice")
        sources[container] = source
    return sources


class WorkloadMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    annotations: dict[str, str] = Field(default_factory=dict)


class WorkloadContainer(BaseModel):
    model_config = ConfigDict(extra="allow")

    image: str | None = None
    files: dict[str, Yaml] = Field(default_factory=dict)
    readiness_probe: WorkloadProbe | None = Field(default=None, alias="readinessProbe")
    liveness_probe: WorkloadProbe | None = Field(default=None, alias="livenessProbe")

    @field_validator("image")
    @classmethod
    def _refuse_authored_image(cls, value: str | None) -> str | None:
        if value is not None:
            raise ValueError(
                f"image is never authored in a workload file; declare {DOCKERFILE_ANNOTATION} "
                f"or {LOCK_ANNOTATION} instead"
            )
        return value


class WorkloadResource(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


class WorkloadPortSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    port: int
    target_port: int | None = Field(default=None, alias="targetPort")


class WorkloadService(BaseModel):
    model_config = ConfigDict(extra="allow")

    ports: dict[str, int | WorkloadPortSpec] = Field(default_factory=dict)


class WorkloadProbe(BaseModel):
    model_config = ConfigDict(extra="allow")

    http_get: Yaml | None = Field(default=None, alias="httpGet")


class LoadedWorkload(NamedTuple):
    file_name: str
    text: str
    raw: dict[str, Yaml]
    document: WorkloadDocument

    @classmethod
    def load(cls, path: Path, remote_bases: dict[str, str] | None) -> LoadedWorkload:
        # remote_bases None skips the lock-key law for the generate-time fixtures that inject their own edges
        if not path.is_file():
            raise ValueError(f"{path.as_posix()}: the workload file is required")
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
        if not is_mapping(raw):
            raise ValueError(f"{path.as_posix()}: a workload file is a mapping")
        document = WorkloadDocument.parse(raw, path.name)
        if remote_bases is not None:
            for container, (kind, value) in document.container_sources().items():
                if kind == "lock" and value not in remote_bases:
                    raise ValueError(f"{path.name} container {container!r}: {value!r} names no remote base_images key")
        return cls(path.name, text, raw, document)


class RouteClaim(NamedTuple):
    path: str
    port_name: str | None
    strip_prefix: bool = False


class ResolvedRoute(NamedTuple):
    path: str
    port: int
    strip_prefix: bool = False


class WorkloadPublishedPort(NamedTuple):
    published: int
    target: int
    protocol: Literal["tcp", "udp"] = "tcp"


def require_compose_probe_adapters(workloads: list[LoadedWorkload]) -> None:
    # Plain score-compose silently drops httpGet probes; this check makes the omission loud
    # before the compose rendering runs (the k8s rendering renders probes natively).
    for workload in workloads:
        if workload.document.metadata.annotations.get(PROBE_ADAPTER_ANNOTATION) is not None:
            continue
        for container in workload.document.containers.values():
            for probe, block in (
                ("readinessProbe", container.readiness_probe),
                ("livenessProbe", container.liveness_probe),
            ):
                if block is not None and block.http_get is not None:
                    raise ValueError(
                        f"{workload.file_name}: a {probe} httpGet requires {PROBE_ADAPTER_ANNOTATION} "
                        "(the compose rendering)"
                    )


def select_provisioner_files(
    score_dir: Path,
    rendering: str,
    workloads: list[LoadedWorkload],
) -> list[tuple[str, list[Yaml]]]:
    # The rendering's distinct files across the workloads' own selections
    selected: dict[str, list[Yaml]] = {}
    for workload in workloads:
        for name, entries in workload.document.provisioners(score_dir, rendering):
            selected.setdefault(name, entries)
    return [(name, selected[name]) for name in sorted(selected)]


def parse_route_annotation(value: str) -> RouteClaim:
    entries = value.split(",")
    strip_prefix = False
    if entries[-1].strip() == "strip":
        strip_prefix = True
        entries = entries[:-1]
    if len(entries) != 1:
        raise ValueError(f"{ROUTE_ANNOTATION} claims exactly one path, plus an optional ', strip' entry")
    claim = entries[0]
    if not claim or claim != claim.strip():
        raise ValueError(f"{ROUTE_ANNOTATION} entry {claim!r} must be non-empty and unpadded")
    if "=" in claim:
        raise ValueError(
            f"{ROUTE_ANNOTATION} entry {claim!r}: the path=host:port route table grammar is removed; "
            "claim one path, with an optional @port-name qualifier and an optional ', strip' entry"
        )
    path, separator, port_name = claim.partition("@")
    if ROUTE_PATH_PATTERN.fullmatch(path) is None:
        raise ValueError(f"{ROUTE_ANNOTATION} path {path!r} must be a /-rooted segment chain, optionally wildcarded")
    if separator and ROUTE_PORT_NAME_PATTERN.fullmatch(port_name) is None:
        raise ValueError(f"{ROUTE_ANNOTATION} port qualifier {port_name!r} must be a plain port name")
    if strip_prefix and not path.endswith("/*"):
        raise ValueError(f"{ROUTE_ANNOTATION}: strip requires the path to end in /* ({path})")
    return RouteClaim(path=path, port_name=port_name if separator else None, strip_prefix=strip_prefix)


def resolve_route_port(claim: RouteClaim, ports: dict[str, int | WorkloadPortSpec]) -> int:
    if not ports:
        raise ValueError(
            f"{ROUTE_ANNOTATION} claims {claim.path} but the workload declares no service.ports stanza; "
            "add the listen port the claim resolves against"
        )
    if claim.port_name is None:
        if len(ports) != 1:
            raise ValueError(
                f"{ROUTE_ANNOTATION} claims {claim.path} with no port qualifier but service.ports declares "
                f"several ({', '.join(sorted(ports))}); name one with the @ qualifier"
            )
        return listen_port(next(iter(ports.values())))
    if claim.port_name not in ports:
        raise ValueError(
            f"{ROUTE_ANNOTATION} port qualifier @{claim.port_name} names no service.ports entry "
            f"({', '.join(sorted(ports))})"
        )
    return listen_port(ports[claim.port_name])


def listen_port(spec: int | WorkloadPortSpec) -> int:
    if isinstance(spec, WorkloadPortSpec):
        return spec.target_port if spec.target_port is not None else spec.port
    return spec


def parse_route_port_annotation(value: str) -> int:
    if value.isdigit() and 1 <= int(value) <= 65535:
        return int(value)
    raise ValueError(f"{ROUTE_PORT_ANNOTATION} {value!r} must be a literal port in the range 1-65535")


def parse_container_ports_annotation(value: str) -> dict[str, int]:
    if not value.strip():
        raise ValueError(f"{CONTAINER_PORTS_ANNOTATION} must be a non-empty comma-separated list of name=port pairs")
    ports: dict[str, int] = {}
    for entry in value.split(","):
        name, separator, port = entry.partition("=")
        padded = name != name.strip() or port != port.strip()
        if not separator or padded or not name or not port:
            raise ValueError(f"{CONTAINER_PORTS_ANNOTATION} entry {entry!r} must be an unpadded name=port pair")
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError(
                f"{CONTAINER_PORTS_ANNOTATION} entry {entry!r} must carry a literal port in the range 1-65535"
            )
        if name in ports:
            raise ValueError(f"{CONTAINER_PORTS_ANNOTATION} names the listener {name!r} twice")
        ports[name] = int(port)
    return ports


def route_path_export_name(workload_name: str) -> str:
    return workload_name.upper().replace("-", "_") + "_" + ROUTE_PATH_EXPORT_SUFFIX


def parse_publish_port_annotation(value: str) -> list[WorkloadPublishedPort]:
    published_ports: list[WorkloadPublishedPort] = []
    for entry in value.split(","):
        if not entry or entry != entry.strip():
            raise ValueError(f"{PUBLISH_PORT_ANNOTATION} entry {entry!r} must be non-empty and unpadded")
        ports, separator, protocol = entry.partition("/")
        if not separator:
            protocol = "tcp"
        if protocol not in PUBLISH_PORT_PROTOCOLS:
            raise ValueError(
                f"{PUBLISH_PORT_ANNOTATION} protocol {protocol!r} must be one of {list(PUBLISH_PORT_PROTOCOLS)}"
            )
        published, port_separator, target = ports.partition(":")
        if not port_separator:
            raise ValueError(f"{PUBLISH_PORT_ANNOTATION} entry {entry!r} must be a published:target pair")
        published_ports.append(
            WorkloadPublishedPort(
                published=_publish_port(entry, published),
                target=_publish_port(entry, target),
                protocol=protocol,
            )
        )
    return published_ports


def _publish_port(entry: str, value: str) -> int:
    if value.isdigit() and 1 <= int(value) <= 65535:
        return int(value)
    raise ValueError(f"{PUBLISH_PORT_ANNOTATION} entry {entry!r} carries the port {value!r} outside 1-65535")
