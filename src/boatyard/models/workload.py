from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from boatyard.models.image import require_dockerfile_spelling
from boatyard.yaml import Yaml, is_list, is_mapping, parse_yaml_text

PROBE_ADAPTER_ANNOTATION = "boatyard/probe-adapter"
PROBE_ADAPTERS = ("python-urllib", "bash-http")
RESTART_ANNOTATION = "boatyard/restart"
COMPOSE_RESTART_POLICIES = ("no", "always", "on-failure", "unless-stopped")
AUTOSTART_ANNOTATION = "boatyard/autostart"

# Interchange labels carrying a resolved claim or publish set into the bundle-record extraction
ROUTE_PATH_LABEL = "boatyard/route-path"
ROUTE_PORT_LABEL = "boatyard/route-port"
ROUTE_STRIP_LABEL = "boatyard/route-strip"
PUBLISH_PORT_LABEL = "boatyard/publish-port"

GPU_ANNOTATION = "boatyard/gpu"

# The closed boatyard/* label vocabulary the bundle-record extraction accepts off a rendered stanza
STANZA_LABELS = (
    ROUTE_PATH_LABEL,
    ROUTE_PORT_LABEL,
    ROUTE_STRIP_LABEL,
    PUBLISH_PORT_LABEL,
    PROBE_ADAPTER_ANNOTATION,
    RESTART_ANNOTATION,
    AUTOSTART_ANNOTATION,
    GPU_ANNOTATION,
)

SCALAR_ANNOTATIONS = (PROBE_ADAPTER_ANNOTATION, RESTART_ANNOTATION, AUTOSTART_ANNOTATION, GPU_ANNOTATION)

ROUTE_PATH_PATTERN = re.compile(r"(?:/\*|/(?:[^/\s*]+(?:/[^/\s*]+)*)?(?:/\*)?)")

LISTENER_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")

LISTENER_REFERENCE_PREFIX = "${listeners."

LISTENER_PORT_LOOKUP = "port"


def _default_metadata() -> WorkloadMetadata:
    return WorkloadMetadata()


class WorkloadDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    metadata: WorkloadMetadata = Field(default_factory=_default_metadata)
    service: Yaml | None = None
    boatyard: BoatyardBlock | None = None
    listeners: dict[str, int | ListenerSpec] = Field(default_factory=dict)
    containers: dict[str, WorkloadContainer] = Field(default_factory=dict)
    resources: dict[str, WorkloadResource] = Field(default_factory=dict)

    @field_validator("service")
    @classmethod
    def _refuse_authored_service(cls, value: Yaml | None) -> Yaml | None:
        if value is not None:
            raise ValueError("service: is compiler output derived from listeners: (author the listeners block instead)")
        return value

    @field_validator("listeners", mode="before")
    @classmethod
    def _refuse_listener_booleans(cls, value: Yaml) -> Yaml:
        # Pydantic coerces True to the int 1 before after-mode validators run
        if is_mapping(value):
            for listener_name, spec in value.items():
                match spec:
                    case bool():
                        raise ValueError(f"the listener {listener_name!r} port must be an integer, not a boolean")
                    case _:
                        pass
        return value

    @field_validator("listeners")
    @classmethod
    def _require_listener_shape(cls, value: dict[str, int | ListenerSpec]) -> dict[str, int | ListenerSpec]:
        # The spec member carries its own port-range validator, so only the bare-int branch checks here
        for listener_name, spec in value.items():
            if LISTENER_NAME_PATTERN.fullmatch(listener_name) is None:
                raise ValueError(f"the listener name {listener_name!r} must be a plain name")
            if isinstance(spec, int):
                require_port_range(spec, f"the listener {listener_name!r}")
        return value

    @model_validator(mode="after")
    def _validate_deployment_surface(self, info: ValidationInfo) -> Self:
        source = info.context.get("source") if info.context is not None else None
        if source is not None and self.metadata.name != Path(source).stem:
            raise ValueError(f"metadata.name must be {Path(source).stem!r} to match the workload file name")

        annotations = self.metadata.annotations
        unknown = sorted(key for key in annotations if key.startswith("boatyard/") and key not in SCALAR_ANNOTATIONS)
        if unknown:
            raise ValueError(
                f"{unknown[0]} is not a workload annotation (the scalar annotations are "
                f"{list(SCALAR_ANNOTATIONS)}, compound facts live in the boatyard: block)"
            )

        adapter = annotations.get(PROBE_ADAPTER_ANNOTATION)
        if adapter is not None and adapter not in PROBE_ADAPTERS:
            raise ValueError(
                f"{PROBE_ADAPTER_ANNOTATION} must be one of the supported adapters {list(PROBE_ADAPTERS)}, not {adapter!r}"
            )

        restart = annotations.get(RESTART_ANNOTATION)
        if restart is not None and restart not in COMPOSE_RESTART_POLICIES:
            raise ValueError(
                f"{RESTART_ANNOTATION} must be one of the compose restart policies "
                f"{list(COMPOSE_RESTART_POLICIES)}, not {restart!r}"
            )

        claim = self.boatyard.route if self.boatyard is not None else None
        if claim is not None:
            routable = self.routable_listener_ports
            if claim.port not in routable:
                raise ValueError(
                    f"the route port {claim.port} names no non-internal listener ({', '.join(sorted(routable))})"
                )

        listeners = self.listener_ports
        for name, container in self.containers.items():
            for probe, block in (
                ("readinessProbe", container.readiness_probe),
                ("livenessProbe", container.liveness_probe),
            ):
                if block is None or block.http_get is None:
                    continue
                http_get = block.http_get
                if not http_get.port:
                    raise ValueError(
                        f"the container {name!r} {probe} httpGet declares no port (name a listeners entry)"
                    )
                if http_get.port not in listeners:
                    raise ValueError(
                        f"the container {name!r} {probe} httpGet port {http_get.port!r} names no declared "
                        f"listener ({', '.join(sorted(listeners))})"
                    )

            variables = container.variables
            if variables is None:
                continue
            for key, value in variables.items():
                if not isinstance(value, str) or not value.startswith(LISTENER_REFERENCE_PREFIX):
                    continue
                listener = listener_port_reference(value)
                if listener is None:
                    raise ValueError(
                        f"the container {name!r} variables {key} value {value!r} must be "
                        f"${{listeners.<listener>.{LISTENER_PORT_LOOKUP}}} naming a listeners entry"
                    )
                if listener not in listeners:
                    raise ValueError(
                        f"the container {name!r} variables {key} value {value!r} names no declared "
                        f"listener ({', '.join(sorted(listeners))})"
                    )

        sources = self.container_sources()
        unsourced = sorted(set(self.containers) - sources.keys())
        if unsourced:
            raise ValueError(
                f"the container {unsourced[0]!r} carries no source in the boatyard block (dockerfile or lock)"
            )
        unknown = sorted(set(sources) - set(self.containers))
        if unknown:
            raise ValueError(f"the boatyard block names no container {unknown[0]!r} of this workload")

        if self.boatyard is None:
            return self
        declared_files = {target for container in self.containers.values() for target in container.files}
        for entry in self.boatyard.consumer_interpolated:
            if entry not in declared_files:
                raise ValueError(f"the consumer-interpolated entry {entry!r} names no declared files")

        return self

    @property
    def listener_ports(self) -> dict[str, int]:
        return {name: spec if isinstance(spec, int) else spec.port for name, spec in self.listeners.items()}

    @property
    def routable_listener_ports(self) -> dict[str, int]:
        # Drop the internal listeners the derived service.ports never carry
        return {
            name: spec if isinstance(spec, int) else spec.port
            for name, spec in self.listeners.items()
            if isinstance(spec, int) or not spec.internal
        }

    @property
    def resolved_route(self) -> ResolvedRoute | None:
        if self.boatyard is None or self.boatyard.route is None:
            return None
        claim = self.boatyard.route
        return ResolvedRoute(
            path=claim.path,
            port=self.routable_listener_ports[claim.port],
            strip_prefix=claim.strip,
        )

    def container_sources(self) -> dict[str, tuple[str, str]]:
        if self.boatyard is None:
            return {}
        return {
            **{name: ("dockerfile", value) for name, value in self.boatyard.dockerfile.items()},
            **{name: ("lock", value) for name, value in self.boatyard.lock.items()},
        }

    def provisioners(self, score_dir: Path, rendering: str) -> list[tuple[str, list[Yaml]]]:
        # A provisioner file joins this workload when one of its entries names a declared resource type
        referenced = {resource.type for resource in self.resources.values()}
        rendering_dir = score_dir / rendering
        if not rendering_dir.is_dir():
            return []
        selected: list[tuple[str, list[Yaml]]] = []
        for path in sorted(rendering_dir.glob("*.yaml")):
            entries = parse_yaml_text(path.read_text(encoding="utf-8"), path.as_posix())
            if is_list(entries) and any(is_mapping(entry) and entry.get("type") in referenced for entry in entries):
                selected.append((f"{rendering}/{path.name}", entries))
        return selected


class BoatyardBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: WorkloadRouteClaim | None = None
    publish: list[PublishSpec] = Field(default_factory=list)
    dockerfile: dict[str, str] = Field(default_factory=dict)
    lock: dict[str, str] = Field(default_factory=dict)
    consumer_interpolated: list[str] = Field(default_factory=list)

    @field_validator("dockerfile")
    @classmethod
    def _require_dockerfile_spelling(cls, value: dict[str, str]) -> dict[str, str]:
        for spelling in value.values():
            require_dockerfile_spelling(spelling)
        return value

    @field_validator("consumer_interpolated")
    @classmethod
    def _require_unpadded_entries(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not entry.strip():
                raise ValueError(f"the consumer-interpolated entry {entry!r} must be non-empty and unpadded")
        return value

    @model_validator(mode="after")
    def _require_disjoint_sources(self) -> Self:
        both = sorted(self.dockerfile.keys() & self.lock.keys())
        if both:
            raise ValueError(f"the container {both[0]!r} is named by both dockerfile and lock")
        return self


# The path/strip shape shared by both claim surfaces, each subclass typing its port
class RouteClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    strip: bool = False

    @model_validator(mode="after")
    def _require_wildcard_strip(self) -> Self:
        if ROUTE_PATH_PATTERN.fullmatch(self.path) is None:
            raise ValueError(
                f"the route path {self.path!r} must be a /-rooted segment chain, optionally wildcarded as /*"
            )
        if self.strip and not self.path.endswith("/*"):
            raise ValueError(f"the route {self.path}: strip requires the path to end in /*")
        return self


class WorkloadRouteClaim(RouteClaim):
    port: str


class PublishSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    published: int
    target: int
    protocol: Literal["tcp", "udp"] = "tcp"

    @field_validator("published", "target", mode="before")
    @classmethod
    def _refuse_boolean_ports(cls, value: Yaml, info: ValidationInfo) -> Yaml:
        match value:
            case bool():
                raise ValueError(f"publish.{info.field_name} must be an integer, not a boolean")
            case _:
                return value

    @field_validator("published", "target")
    @classmethod
    def _require_port_range(cls, value: int, info: ValidationInfo) -> int:
        return require_port_range(value, f"publish.{info.field_name}")


class ListenerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int
    internal: bool = False

    @field_validator("port", mode="before")
    @classmethod
    def _refuse_boolean_port(cls, value: Yaml) -> Yaml:
        match value:
            case bool():
                raise ValueError("listeners.port must be an integer, not a boolean")
            case _:
                return value

    @field_validator("port")
    @classmethod
    def _require_port_range(cls, value: int) -> int:
        return require_port_range(value, "listeners.port")


class WorkloadMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    annotations: dict[str, str] = Field(default_factory=dict)


class WorkloadContainer(BaseModel):
    model_config = ConfigDict(extra="allow")

    image: str | None = None
    files: dict[str, Yaml] = Field(default_factory=dict)
    variables: dict[str, Yaml] | None = None
    readiness_probe: WorkloadProbe | None = Field(default=None, alias="readinessProbe")
    liveness_probe: WorkloadProbe | None = Field(default=None, alias="livenessProbe")

    @field_validator("image")
    @classmethod
    def _refuse_authored_image(cls, value: str | None) -> str | None:
        if value is not None:
            raise ValueError(
                "image is never authored in a workload file (declare it in the boatyard block, dockerfile or lock)"
            )
        return value


class WorkloadResource(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


class WorkloadProbe(BaseModel):
    model_config = ConfigDict(extra="allow")

    http_get: WorkloadHttpGet | None = Field(default=None, alias="httpGet")


class WorkloadHttpGet(BaseModel):
    model_config = ConfigDict(extra="allow")

    # The absent port spells as the empty string, refused by the document validator
    port: str = ""

    @field_validator("port", mode="before")
    @classmethod
    def _require_listener_name_spelling(cls, value: Yaml) -> Yaml:
        match value:
            case str():
                return value
            case _:
                raise ValueError(
                    f"the httpGet port {value!r}: probe ports are listener names, so name a listeners entry"
                )


class LoadedWorkload(NamedTuple):
    file_name: str
    document: WorkloadDocument


class ResolvedRoute(NamedTuple):
    path: str
    port: int
    strip_prefix: bool = False


WorkloadDocument.model_rebuild()


def select_provisioner_files(
    score_dir: Path,
    rendering: str,
    workloads: list[LoadedWorkload],
) -> list[tuple[str, list[Yaml]]]:
    # Deduplicate the rendering's files across the workloads' own selections
    selected: dict[str, list[Yaml]] = {}
    for workload in workloads:
        for name, entries in workload.document.provisioners(score_dir, rendering):
            selected.setdefault(name, entries)
    return sorted(selected.items())


def require_port_range(value: int, site: str) -> int:
    if not 1 <= value <= 65535:
        raise ValueError(f"{site} carries the port {value!r} outside 1-65535")
    return value


def listener_port_reference(value: str) -> str | None:
    # Match the whole-value grammar ${listeners.<name>.port}, None for any other spelling
    if not value.startswith(LISTENER_REFERENCE_PREFIX) or not value.endswith("}"):
        return None
    name, separator, lookup = value[len(LISTENER_REFERENCE_PREFIX) : -1].partition(".")
    if not separator or not name or lookup != LISTENER_PORT_LOOKUP:
        return None
    return name
