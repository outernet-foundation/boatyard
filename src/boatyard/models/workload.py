from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
import yaml

from boatyard.markers import declared_dockerfile_paths
from boatyard.models.image import dockerfile_stem, require_dockerfile_spelling
from boatyard.yaml import Yaml, is_list, is_mapping

PROBE_ADAPTER_ANNOTATION = "outernet.internal/probe-adapter"
PROBE_ADAPTERS = ("python-urllib", "bash-http")
RESTART_ANNOTATION = "outernet.internal/restart"
COMPOSE_RESTART_POLICIES = ("no", "always", "on-failure", "unless-stopped")
CONSUMER_INTERPOLATED_ANNOTATION = "outernet.internal/consumer-interpolated"
DOCKERFILE_ANNOTATION = "outernet.internal/dockerfile"
LOCK_ANNOTATION = "outernet.internal/lock"
ROUTE_ANNOTATION = "outernet.internal/route"
PUBLISH_ANNOTATION = "outernet.internal/publish"

ROUTE_PATH_PATTERN = re.compile(r"(?:/\*|/(?:[^/\s*]+(?:/[^/\s*]+)*)?(?:/\*)?)")

ROUTE_UPSTREAM_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*:[0-9]+")

PUBLISH_PROTOCOLS = ("tcp", "udp")


def _default_metadata() -> WorkloadMetadata:
    return WorkloadMetadata()


class WorkloadDocument(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    metadata: WorkloadMetadata = Field(default_factory=_default_metadata)
    containers: dict[str, WorkloadContainer] = Field(default_factory=dict)
    resources: dict[str, WorkloadResource] = Field(default_factory=dict)

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
            parse_route_annotation(route)

        publish = annotations.get(PUBLISH_ANNOTATION)
        if publish is not None:
            parse_publish_annotation(publish)

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
    def routes(self) -> list[WorkloadRoute]:
        annotation = self.metadata.annotations.get(ROUTE_ANNOTATION)
        return parse_route_annotation(annotation) if annotation is not None else []

    @property
    def publishes(self) -> list[WorkloadPublish]:
        annotation = self.metadata.annotations.get(PUBLISH_ANNOTATION)
        return parse_publish_annotation(annotation) if annotation is not None else []

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


class WorkloadRoute(NamedTuple):
    path: str
    upstream: str
    strip_prefix: bool = False


class WorkloadPublish(NamedTuple):
    published: int
    target: int
    protocol: str = "tcp"


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


def parse_route_annotation(value: str) -> list[WorkloadRoute]:
    routes: list[WorkloadRoute] = []
    for entry in value.split(","):
        if not entry or entry != entry.strip():
            raise ValueError(f"{ROUTE_ANNOTATION} entry {entry!r} must be non-empty and unpadded")
        if entry == "strip":
            if not routes:
                raise ValueError(f"{ROUTE_ANNOTATION} entry 'strip' attaches to no preceding route")
            if not routes[-1].path.endswith("/*"):
                raise ValueError(f"{ROUTE_ANNOTATION}: strip requires the path to end in /* ({routes[-1].path})")
            routes[-1] = routes[-1]._replace(strip_prefix=True)
            continue
        path, separator, upstream = entry.partition("=")
        if not separator:
            raise ValueError(f"{ROUTE_ANNOTATION} entry {entry!r} must be a path=host:port pair")
        if ROUTE_PATH_PATTERN.fullmatch(path) is None:
            raise ValueError(
                f"{ROUTE_ANNOTATION} path {path!r} must be a /-rooted segment chain, optionally wildcarded"
            )
        if ROUTE_UPSTREAM_PATTERN.fullmatch(upstream) is None:
            raise ValueError(f"{ROUTE_ANNOTATION} upstream {upstream!r} must be a service name and a literal port")
        routes.append(WorkloadRoute(path=path, upstream=upstream))
    return routes


def parse_publish_annotation(value: str) -> list[WorkloadPublish]:
    publishes: list[WorkloadPublish] = []
    for entry in value.split(","):
        if not entry or entry != entry.strip():
            raise ValueError(f"{PUBLISH_ANNOTATION} entry {entry!r} must be non-empty and unpadded")
        ports, separator, protocol = entry.partition("/")
        if not separator:
            protocol = "tcp"
        if protocol not in PUBLISH_PROTOCOLS:
            raise ValueError(f"{PUBLISH_ANNOTATION} protocol {protocol!r} must be one of {list(PUBLISH_PROTOCOLS)}")
        published, port_separator, target = ports.partition(":")
        if not port_separator:
            raise ValueError(f"{PUBLISH_ANNOTATION} entry {entry!r} must be a published:target pair")
        publishes.append(
            WorkloadPublish(
                published=_publish_port(entry, published),
                target=_publish_port(entry, target),
                protocol=protocol,
            )
        )
    return publishes


def _publish_port(entry: str, value: str) -> int:
    if value.isdigit() and 1 <= int(value) <= 65535:
        return int(value)
    raise ValueError(f"{PUBLISH_ANNOTATION} entry {entry!r} carries the port {value!r} outside 1-65535")
