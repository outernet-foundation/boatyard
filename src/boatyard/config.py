from __future__ import annotations

import importlib.metadata
import re
import tomllib
from itertools import chain
from pathlib import Path
from typing import Annotated, Literal, Self, TypeIs

from packaging.specifiers import SpecifierSet
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationInfo,
    field_validator,
    model_validator,
)
from strictyaml import load as load_strict_yaml
from yaml import YAMLError, safe_load

from boatyard.config_lock import (
    LOCK_PATH,
    PLATFORM_PATTERN,
    SHA256_DIGEST_REGEX,
    read_config_lock,
)
from boatyard.dockerfiles import (
    DOCKERFILE_NAME,
    ServiceBuild,
    derive_services,
    service_sha_env,
)

DEFAULT_CONFIG_PATH = Path("boatyard.yml")
DEV_SENTINEL = "0.0.0.dev0"

DEFAULT_MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"

PYPROJECT_PATH = Path("pyproject.toml")
PYTHON_VERSION_PATH = Path(".python-version")

ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Match ${ALL_CAPS} placeholder holes in workload files.
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

# Match a compose brace hole with its preceding dollar run.
COMPOSE_HOLE = re.compile(r"(\$+)\{([A-Z][A-Z0-9_]*)(?::[^{}]*)?\}")

type Yaml = str | int | float | bool | list[Yaml] | dict[str, Yaml] | None


def _require_port_or_hole(value: str) -> str:
    if PORT_PATTERN.fullmatch(value) is None and ENV_HOLE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{value!r} must be a literal port number or a ${{…}} env hole")
    return value


PortOrHole = Annotated[str, AfterValidator(_require_port_or_hole)]


def _require_all_caps_name(value: str) -> str:
    if ENV_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{value!r} must be an ALL_CAPS env name")
    return value


AllCapsName = Annotated[str, AfterValidator(_require_all_caps_name)]

# Match path:tag or path@digest.
DECLARED_REFERENCE_PATTERN = re.compile(
    rf"^(?P<path>[^:@\s$]+)(?::(?P<tag>[^@\s$]+))?(?:@(?P<digest>{SHA256_DIGEST_REGEX}))?$"
)

TEMPLATE_KEY_PATTERN = re.compile(r"\{([a-z][a-z0-9_]*)\}")

# Match the range operators a ref tag must never carry; exact tags only.
REF_TAG_OPERATOR_PATTERN = re.compile(r"[<>=!~^*]")

OCI_REFERENCE_PREFIX = "oci://"

# Match compose env holes: ${NAME}, ${NAME:?err}, ${NAME:-default}.
ENV_HOLE_PATTERN = re.compile(r"^\$\{[A-Z][A-Z0-9_]*(?::[^{}]*)?\}$")
PORT_PATTERN = re.compile(r"^[0-9]+$")

AUTH_GROUP_NAME = "auth"
EDGE_SERVICE_NAME = "edge"
CADDY_IMAGE = "CADDY_IMAGE"
DEV_TOGGLES_ANNOTATION = "outernet.internal/dev-toggles"

COMPOSE_OUTPUT_DIR = Path("stack/generated/compose")

Target = Literal["both", "compose", "k8s"]


class UvToolTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    required_version: str | None = Field(default=None, alias="required-version")


class ToolTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uv: UvToolTable | None = None


class PyprojectToml(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool: ToolTable | None = None


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requires: str
    gateway: PortOrHole | None = None
    repository: str | None = None
    base_images: dict[AllCapsName, str] = Field(default_factory=dict)
    variants: dict[AllCapsName, list[str]] = Field(default_factory=dict)
    platforms: list[str] = Field(default_factory=list)
    project_name: str | None = None
    services: dict[str, ServiceMember] = Field(default_factory=dict)
    k8s: K8sConfig | None = None
    mirror_prefix: str = DEFAULT_MIRROR_PREFIX

    score_dir: Path = Path("stack/score")
    k8s_output: Path = Path("stack/generated/k8s/manifests.yaml")
    k8s_local_output: Path = Path("stack/generated/k8s/manifests.local.yaml")
    score_compose_version: str = "0.42.0"
    score_k8s_version: str = "0.15.0"
    helm_version: str = "3.22.1"

    service_builds: dict[str, ServiceBuild] = Field(default_factory=dict, exclude=True)
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    @classmethod
    def load(cls, root: Path) -> Config | None:
        path = root / DEFAULT_CONFIG_PATH
        if not path.exists():
            return None
        config = cls.model_validate(load_strict_yaml(path.read_text(encoding="utf-8")).data)
        config.resolve(root)
        return config

    def resolve(self, root: Path) -> Self:
        # Self-check the installed version.
        installed = importlib.metadata.version("boatyard")
        if installed != DEV_SENTINEL and not SpecifierSet(self.requires).contains(installed, prereleases=True):
            raise ValueError(f"boatyard {installed} does not satisfy requires={self.requires!r}")
        self.root = root

        # Resolve {uv} and {python} templates in base_images against the toolchain pins.
        pins: dict[str, str] = {}
        used = sorted({
            key for reference in self.base_images.values() for key in TEMPLATE_KEY_PATTERN.findall(reference)
        })
        for key in used:
            if key == "uv":
                pyproject = root / PYPROJECT_PATH
                pyproject_document = PyprojectToml.model_validate(
                    tomllib.loads(pyproject.read_text(encoding="utf-8")) if pyproject.is_file() else {}
                )
                declared = (
                    pyproject_document.tool.uv.required_version
                    if pyproject_document.tool is not None and pyproject_document.tool.uv is not None
                    else None
                )
                if not declared:
                    raise ValueError(
                        f"{PYPROJECT_PATH.as_posix()}: tool.uv.required-version is not declared; "
                        "the {uv} template requires it"
                    )
                pin_match = re.fullmatch(r"==(?P<version>[^,*\s]+)", declared.strip())
                if pin_match is None:
                    raise ValueError(
                        f"{PYPROJECT_PATH.as_posix()}: tool.uv.required-version {declared!r} "
                        "must be an exact == pin like '==0.12.15'"
                    )
                pins[key] = pin_match.group("version")
            elif key == "python":
                python_version = root / PYTHON_VERSION_PATH
                if not python_version.is_file():
                    raise ValueError(
                        f"{PYTHON_VERSION_PATH.as_posix()}: the file is absent; the {{{key}}} template requires it"
                    )
                # Take the first non-empty line.
                version = next(
                    (line.strip() for line in python_version.read_text(encoding="utf-8").splitlines() if line.strip()),
                    "",
                )
                if not version:
                    raise ValueError(
                        f"{PYTHON_VERSION_PATH.as_posix()}: the file is empty; the {{{key}}} template requires it"
                    )
                pins[key] = version
            else:
                raise ValueError(
                    f"base_images template key {{{key}}} is unknown; the toolchain pins are {{uv}}, {{python}}"
                )
        self.base_images = {
            name: TEMPLATE_KEY_PATTERN.sub(lambda match: pins[match.group(1)], reference)
            for name, reference in self.base_images.items()
        }

        # Derive the service build set from the Dockerfile tree.
        service_builds = derive_services(root, self.variants)

        computable = {service_sha_env(name) for name in service_builds}
        for service in service_builds.values():
            unknown = [argument for argument in service.sha_args if argument not in computable]
            if unknown:
                raise ValueError(
                    f"workloads/{service.name}/{DOCKERFILE_NAME}: ARG {', '.join(unknown)} names no service directory"
                )
        if service_builds and self.repository is None:
            listed = ", ".join(sorted(service_builds))
            raise ValueError(f"Dockerfile service directories exist ({listed}); repository is required")
        self.service_builds = service_builds

        # Enforce the lock pairing laws.
        declared_charts = self.declared_charts
        if (self.base_images or declared_charts or self.remote_service_entries()) and not (root / LOCK_PATH).is_file():
            raise ValueError(
                f"{LOCK_PATH.as_posix()}: base_images, k8s.charts, or service refs are declared; "
                "the committed lock is required"
            )
        declares_routes = any(
            entry.routes
            or (isinstance(entry, ServiceGroup) and any(option.routes for option in entry.options.values()))
            for entry in self.services.values()
        )
        if declares_routes:
            read_config_lock(root).require(
                CADDY_IMAGE,
                "routes are declared",
                hint="declare CADDY_IMAGE in base_images and run lock",
            )
        for chart in declared_charts:
            values = root / self.score_dir / "k8s" / "values" / f"{chart}.yaml"
            if not values.is_file():
                raise ValueError(f"{values.as_posix()}: the chart's values file is required")

        return self

    @property
    def declared_charts(self) -> dict[str, str]:
        return self.k8s.charts if self.k8s is not None else {}

    @property
    def selectable_options(self) -> dict[str, tuple[str, str]]:
        return {
            qualified_option_name(name, option_name): (name, option_name)
            for name, entry in self.services.items()
            if isinstance(entry, ServiceGroup)
            for option_name in entry.options
        }

    @property
    def bare_entries(self) -> list[str]:
        return [name for name, entry in self.services.items() if not isinstance(entry, ServiceGroup)]

    def _flattened_service_entries(self) -> list[tuple[str, ServiceEntry]]:
        # Options ride under their qualified name; bare entries are their own root.
        flattened = chain.from_iterable(
            (
                ((qualified_option_name(name, option_name), option) for option_name, option in entry.options.items())
                if isinstance(entry, ServiceGroup)
                else ((name, entry),)
            )
            for name, entry in self.services.items()
        )
        return list(flattened)

    def local_service_entries(self) -> list[tuple[str, ServiceEntry]]:
        return [(name, entry) for name, entry in self._flattened_service_entries() if not entry.remote]

    def remote_service_entries(self) -> list[tuple[str, str]]:
        return [(name, entry.ref) for name, entry in self._flattened_service_entries() if entry.ref is not None]

    @field_validator("requires")
    @classmethod
    def _validate_requires(cls, value: str) -> str:
        if not value:
            raise ValueError("requires must be a non-empty PEP 440 specifier")
        SpecifierSet(value)
        return value

    @field_validator("repository")
    @classmethod
    def _validate_repository(cls, value: str | None) -> str | None:
        if value is not None:
            host, separator, path = value.partition("/")
            if not separator or not host or not path or value.endswith("/"):
                raise ValueError(
                    "repository must be registry-host/repository-path with no trailing slash (ghcr.io/org/repo)"
                )
        return value

    @field_validator("base_images")
    @classmethod
    def _validate_base_images(cls, value: dict[AllCapsName, str]) -> dict[AllCapsName, str]:
        for name, reference in value.items():
            match = DECLARED_REFERENCE_PATTERN.fullmatch(reference)
            if match is None or not (match.group("tag") or match.group("digest")):
                raise ValueError(f"base_images.{name}: {reference!r} must be 'path:tag' or 'path@digest'")
        return value

    @field_validator("variants")
    @classmethod
    def _validate_variants(cls, value: dict[AllCapsName, list[str]]) -> dict[AllCapsName, list[str]]:
        for axis, values in value.items():
            if not values:
                raise ValueError(f"variants.{axis}: must declare at least one value")
            seen: set[str] = set()
            for cell in values:
                if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", cell) is None:
                    raise ValueError(f"variants.{axis}: value {cell!r} must be a bare lowercase spelling like cuda")
                if cell in seen:
                    raise ValueError(f"variants.{axis}: value {cell!r} is declared twice")
                seen.add(cell)
        return value

    @field_validator("platforms")
    @classmethod
    def _validate_platforms(cls, value: list[str]) -> list[str]:
        for platform in value:
            if PLATFORM_PATTERN.fullmatch(platform) is None:
                raise ValueError(f"platforms entry {platform!r} must be an os/arch pair like linux/arm64")
        return value

    @field_validator("mirror_prefix")
    @classmethod
    def _validate_mirror_prefix(cls, value: str) -> str:
        if not value or value.endswith("/"):
            raise ValueError("mirror_prefix must be non-empty with no trailing slash")
        return value

    @field_validator("services")
    @classmethod
    def _validate_services(
        cls, services: dict[str, ServiceGroup | ServiceEntry]
    ) -> dict[str, ServiceGroup | ServiceEntry]:
        # The qualified vocabulary (options as group-dash-option, bare entries as their root
        # name) must be collision-free: two declaration sites never produce one spelling.
        vocabulary: dict[str, str] = {}
        for name, entry in services.items():
            if isinstance(entry, ServiceGroup):
                sites = {
                    qualified_option_name(name, option_name): f"services.{name}.options.{option_name}"
                    for option_name in entry.options
                }
            else:
                sites = {name: f"services.{name}"}
            for spelling, site in sites.items():
                if spelling in vocabulary:
                    raise ValueError(f"{site}: {spelling} is already claimed at {vocabulary[spelling]}")
                vocabulary[spelling] = site

        # Collect every route with its declaration site; paths are edge-wide unique.
        route_sites = [
            (where, route)
            for name, entry in services.items()
            for where, entry_routes in (
                [(f"services.{name}", entry.routes)]
                if not isinstance(entry, ServiceGroup)
                else [
                    (f"services.{name}.options.{option_name}", option.routes)
                    for option_name, option in entry.options.items()
                ]
                + [(f"services.{name}", entry.routes)]
            )
            for route in entry_routes
        ]
        owner: dict[str, str] = {}
        for where, route in route_sites:
            if route.path in owner:
                raise ValueError(
                    f"{where}: route path {route.path} is already declared at {owner[route.path]}; "
                    "paths are edge-wide unique"
                )
            owner[route.path] = where

        if AUTH_GROUP_NAME in services:
            auth = services[AUTH_GROUP_NAME]
            if not isinstance(auth, ServiceGroup):
                raise ValueError(f"services.{AUTH_GROUP_NAME}: auth is a reserved group name; it must be a group")
            for option_name, option in auth.options.items():
                if len(option.routes) != 1:
                    raise ValueError(
                        f"services.{AUTH_GROUP_NAME}.options.{option_name}: an auth option declares exactly one route"
                    )
        return services

    @model_validator(mode="after")
    def _validate(self) -> Self:
        # Require project_name exactly when services are declared.
        if self.services:
            if not self.project_name:
                raise ValueError("services are declared; project_name is required")
            any_local = any(
                not option.remote
                for entry in self.services.values()
                for option in (entry.options.values() if isinstance(entry, ServiceGroup) else (entry,))
            )
            if any_local and not self.repository:
                raise ValueError("local entries (workloads:) are declared; repository is required")

        if self.base_images and not self.platforms:
            raise ValueError(
                "base_images are declared; platforms is required (an amd64-only repo declares [linux/amd64])"
            )
        return self


class ServiceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: dict[str, ServiceEntry]
    routes: list[RouteConfig] = Field(default_factory=list)
    ports: list[PortDeclaration] = Field(default_factory=list)


class ServiceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str | None = None
    workloads: list[str] = Field(default_factory=list)
    vars: dict[str, str] = Field(default_factory=dict)
    routes: list[RouteConfig] = Field(default_factory=list)
    ports: list[PortDeclaration] = Field(default_factory=list)

    @property
    def remote(self) -> bool:
        return self.ref is not None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.ref is not None:
            require_bundle_reference(self.ref)
        if self.ref is not None and self.workloads:
            raise ValueError("an entry declares ref: or workloads:, not both")
        if self.ref is None and not self.workloads:
            raise ValueError("ref: or workloads: is required")
        if self.ref is not None and self.vars:
            raise ValueError("vars is not allowed with ref:")
        for name in self.workloads:
            require_workload_reference(name)
        return self


def _service_member_kind(value: Yaml) -> str:
    return "group" if is_mapping(value) and "options" in value else "entry"


ServiceMember = Annotated[
    Annotated[ServiceGroup, Tag("group")] | Annotated[ServiceEntry, Tag("entry")],
    Discriminator(_service_member_kind),
]


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    upstream: str
    strip_prefix: bool = False
    raw: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if re.fullmatch(r"(?:/\*|/(?:[^/\s*]+(?:/[^/\s*]+)*)?(?:/\*)?)", self.path) is None:
            raise ValueError(f"route path {self.path!r} must be a /-rooted segment chain, optionally wildcarded as …/*")
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*:[0-9]+", self.upstream) is None:
            raise ValueError(
                f"route upstream {self.upstream!r} must be a service name and a literal port (grafana:3000)"
            )
        if self.strip_prefix and not self.path.endswith("/*"):
            raise ValueError(f"route {self.path}: the path must end in /* for strip_prefix")
        return self


class PortDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    published: PortOrHole
    target: PortOrHole
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str | None = None


class K8sConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workloads: list[str] = Field(default_factory=list)
    vars: dict[str, str] = Field(default_factory=dict)
    charts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for name in self.workloads:
            require_workload_reference(name)
        for name, reference in self.charts.items():
            if not name:
                raise ValueError("charts: names must be non-empty")
            if not reference.startswith(OCI_REFERENCE_PREFIX):
                raise ValueError(
                    f"charts.{name}: {reference!r} must be an upstream-native OCI reference "
                    f"({OCI_REFERENCE_PREFIX}host/path:tag or {OCI_REFERENCE_PREFIX}host/path@digest)"
                )
            remainder = reference.removeprefix(OCI_REFERENCE_PREFIX)
            match = DECLARED_REFERENCE_PATTERN.fullmatch(remainder)
            if match is None or not (match.group("tag") or match.group("digest")):
                raise ValueError(f"charts.{name}: {reference!r} must carry a tag or a digest")
        if not (self.workloads or self.charts):
            raise ValueError("the k8s section must declare workloads or charts")
        return self


PROBE_ADAPTER_ANNOTATION = "outernet.internal/probe-adapter"
PROBE_ADAPTERS = ("python-urllib", "bash-http")
RESTART_ANNOTATION = "outernet.internal/restart"
COMPOSE_RESTART_POLICIES = ("no", "always", "on-failure", "unless-stopped")
CONSUMER_INTERPOLATED_ANNOTATION = "outernet.internal/consumer-interpolated"


class WorkloadMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    annotations: dict[str, str] = Field(default_factory=dict)


class WorkloadProbe(BaseModel):
    model_config = ConfigDict(extra="allow")

    http_get: Yaml | None = Field(default=None, alias="httpGet")


class WorkloadContainer(BaseModel):
    model_config = ConfigDict(extra="allow")

    image: str | None = None
    files: dict[str, Yaml] = Field(default_factory=dict)
    readiness_probe: WorkloadProbe | None = Field(default=None, alias="readinessProbe")
    liveness_probe: WorkloadProbe | None = Field(default=None, alias="livenessProbe")

    @field_validator("image")
    @classmethod
    def _require_image_hole(cls, value: str | None) -> str | None:
        if value is not None and "${" not in value and "{{" not in value:
            raise ValueError(f"image {value!r} must bind to the lock or a service sha via a ${{NAME}} hole")
        return value


class WorkloadResource(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


class WorkloadDocument(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    metadata: WorkloadMetadata = Field(default_factory=WorkloadMetadata)
    containers: dict[str, WorkloadContainer] = Field(default_factory=dict)
    resources: dict[str, WorkloadResource] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.parse(load_yaml_file(path), path.name)

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
                f"{PROBE_ADAPTER_ANNOTATION} is {adapter!r}; the supported adapters are {list(PROBE_ADAPTERS)}"
            )

        restart = annotations.get(RESTART_ANNOTATION)
        if restart is not None and restart not in COMPOSE_RESTART_POLICIES:
            raise ValueError(
                f"{RESTART_ANNOTATION} is {restart!r}; the compose restart policies are "
                f"{list(COMPOSE_RESTART_POLICIES)}"
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

        if annotations.get(DEV_TOGGLES_ANNOTATION) is not None:
            if not annotations[DEV_TOGGLES_ANNOTATION].strip():
                raise ValueError(f"{DEV_TOGGLES_ANNOTATION} must be a comma-separated NAME=default string")
            parse_dev_toggles(annotations[DEV_TOGGLES_ANNOTATION])

        return self

    @property
    def consumer_interpolated_targets(self) -> list[str]:
        annotation = self.metadata.annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
        return annotation.split(",") if annotation is not None else []


class ComposeWorkloadDocument(WorkloadDocument):
    @model_validator(mode="after")
    def _require_probe_adapter_for_http_get(self) -> Self:
        if self.metadata.annotations.get(PROBE_ADAPTER_ANNOTATION) is not None:
            return self
        for container in self.containers.values():
            for probe, block in (
                ("readinessProbe", container.readiness_probe),
                ("livenessProbe", container.liveness_probe),
            ):
                if block is not None and block.http_get is not None:
                    raise ValueError(f"a {probe} httpGet requires {PROBE_ADAPTER_ANNOTATION}")
        return self


def require_workload_reference(name: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9._-]+\.yaml", name) is None:
        raise ValueError(f"workload reference {name!r} must be a flat .yaml file name")


def require_bundle_reference(value: str) -> None:
    if not value.startswith(OCI_REFERENCE_PREFIX):
        raise ValueError(f"ref {value!r} must be an OCI include ({OCI_REFERENCE_PREFIX}host/path:tag)")
    remainder = value.removeprefix(OCI_REFERENCE_PREFIX)
    if "@" in remainder:
        raise ValueError(f"ref {value!r} pins a digest; a ref carries a published tag and the lock records the digest")
    reference, separator, tag = remainder.rpartition(":")
    if not separator or not reference or not tag or re.search(r"\s", remainder):
        raise ValueError(f"ref {value!r} must carry a tag ({OCI_REFERENCE_PREFIX}host/path:tag)")
    if tag == "latest":
        raise ValueError(f"ref {value!r}: the tag 'latest' is mutable and never a pin; pin a stable, dev, or sha tag")
    if REF_TAG_OPERATOR_PATTERN.search(tag) or any(part in ("x", "X") for part in tag.split(".")):
        raise ValueError(f"ref {value!r}: the tag {tag!r} is a range; exact tags only")


def compose_bundle_output_path(root: Path, name: str) -> Path:
    return root / COMPOSE_OUTPUT_DIR / f"compose.{name}.yaml"


def qualified_option_name(group: str, option_name: str) -> str:
    return f"{group}-{option_name}"


def load_yaml_file(path: Path) -> Yaml:
    try:
        return safe_load(path.read_text(encoding="utf-8"))
    except YAMLError as error:
        raise ValueError(f"{path.as_posix()}: {error}") from error


def require_compose_stack(config: Config | None) -> Config:
    if config is None or not config.services:
        raise ValueError("boatyard.yml must exist and declare services")
    return config


def chart_lock_key(name: str) -> str:
    return name.upper().replace("-", "_") + "_CHART"


def bundle_lock_key(name: str) -> str:
    return name.upper().replace("-", "_") + "_BUNDLE"


def parse_dev_toggles(raw: str) -> dict[str, str]:
    toggles: dict[str, str] = {}
    for item in raw.split(","):
        toggle_name, separator, default = item.strip().partition("=")
        if not separator:
            raise ValueError(f"{DEV_TOGGLES_ANNOTATION} entry {item.strip()!r} must be NAME=default")
        if ENV_NAME_PATTERN.fullmatch(toggle_name) is None:
            raise ValueError(f"{DEV_TOGGLES_ANNOTATION} name {toggle_name!r} must be ALL_CAPS")
        if not default:
            raise ValueError(f"{DEV_TOGGLES_ANNOTATION} default for {toggle_name} is empty")
        if toggle_name in toggles:
            raise ValueError(f"{DEV_TOGGLES_ANNOTATION} names {toggle_name} twice")
        toggles[toggle_name] = default
    return toggles


def mirrored_ref(reference: str, prefix: str) -> str:
    if reference.startswith(f"{prefix}/"):
        raise ValueError(f"declarations are upstream-native; {reference} already carries the mirror prefix {prefix}")
    return f"{prefix}/{reference}"


def is_mapping(value: object) -> TypeIs[dict[str, Yaml]]:
    return isinstance(value, dict)


def is_list(value: object) -> TypeIs[list[Yaml]]:
    return isinstance(value, list)


def double_dollars(text: str) -> str:
    return text.replace("$", "$$")
