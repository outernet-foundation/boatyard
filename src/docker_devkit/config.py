from __future__ import annotations

import importlib.metadata
import re
import tomllib
from itertools import chain
from pathlib import Path
from typing import Annotated, Literal, NamedTuple, Self, TypeIs

from packaging.specifiers import SpecifierSet
from pydantic import AfterValidator, BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator, model_validator
from strictyaml import load as load_strict_yaml

from docker_devkit.config_lock import (
    LOCK_PATH,
    PLATFORM_PATTERN,
    SHA256_DIGEST_REGEX,
    read_config_lock,
)
from docker_devkit.dockerfiles import (
    DOCKERFILE_NAME,
    ServiceBuild,
    derive_services,
    service_sha_env,
)

DEFAULT_CONFIG_PATH = Path("docker-devkit.yaml")
DEV_SENTINEL = "0.0.0.dev0"

DEFAULT_MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"

PYPROJECT_PATH = Path("pyproject.toml")
PYTHON_VERSION_PATH = Path(".python-version")

ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


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

CHART_REFERENCE_PREFIX = "oci://"

# Match compose env holes: ${NAME}, ${NAME:?err}, ${NAME:-default}.
ENV_HOLE_PATTERN = re.compile(r"^\$\{[A-Z][A-Z0-9_]*(?::[^{}]*)?\}$")
PORT_PATTERN = re.compile(r"^[0-9]+$")

AUTH_GROUP_NAME = "auth"
EDGE_SERVICE_NAME = "edge"
CADDY_IMAGE = "CADDY_IMAGE"
DEV_TOGGLES_ANNOTATION = "outernet.internal/dev-toggles"

Target = Literal["both", "compose", "k8s"]


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
        installed = importlib.metadata.version("docker-devkit")
        if installed != DEV_SENTINEL and not SpecifierSet(self.requires).contains(installed, prereleases=True):
            raise SystemExit(f"docker-devkit {installed} does not satisfy requires={self.requires!r}")
        self.root = root

        # Resolve {uv} and {python} templates in base_images against the toolchain pins.
        pins: dict[str, str] = {}
        used = sorted({
            key for reference in self.base_images.values() for key in TEMPLATE_KEY_PATTERN.findall(reference)
        })
        for key in used:
            if key == "uv":
                pyproject = root / PYPROJECT_PATH
                tool_document: dict[str, object] = (
                    tomllib.loads(pyproject.read_text(encoding="utf-8")) if pyproject.is_file() else {}
                )
                tool_table: object = tool_document.get("tool")
                uv_table: object = tool_table.get("uv") if is_mapping(tool_table) else None
                declared: object = uv_table.get("required-version") if is_mapping(uv_table) else None
                if not isinstance(declared, str) or not declared:
                    raise RuntimeError(
                        f"{PYPROJECT_PATH.as_posix()}: tool.uv.required-version is not declared; "
                        "the {uv} template requires it"
                    )
                pin_match = re.fullmatch(r"==(?P<version>[^,*\s]+)", declared.strip())
                if pin_match is None:
                    raise RuntimeError(
                        f"{PYPROJECT_PATH.as_posix()}: tool.uv.required-version {declared!r} "
                        "must be an exact == pin like '==0.12.15'"
                    )
                pins[key] = pin_match.group("version")
            elif key == "python":
                python_version = root / PYTHON_VERSION_PATH
                if not python_version.is_file():
                    raise RuntimeError(
                        f"{PYTHON_VERSION_PATH.as_posix()}: the file is absent; the {{{key}}} template requires it"
                    )
                # Take the first non-empty line.
                version = next(
                    (line.strip() for line in python_version.read_text(encoding="utf-8").splitlines() if line.strip()),
                    "",
                )
                if not version:
                    raise RuntimeError(
                        f"{PYTHON_VERSION_PATH.as_posix()}: the file is empty; the {{{key}}} template requires it"
                    )
                pins[key] = version
            else:
                raise RuntimeError(
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
                raise RuntimeError(
                    f"workloads/{service.name}/{DOCKERFILE_NAME}: ARG {', '.join(unknown)} names no service directory"
                )
        if service_builds and self.repository is None:
            listed = ", ".join(sorted(service_builds))
            raise RuntimeError(f"Dockerfile service directories exist ({listed}); repository is required")
        self.service_builds = service_builds

        # Enforce the lock pairing laws.
        declared_charts = self.declared_charts
        if (self.base_images or declared_charts) and not (root / LOCK_PATH).is_file():
            raise RuntimeError(
                f"{LOCK_PATH.as_posix()}: base_images or k8s.charts are declared; the committed lock is required"
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
                raise RuntimeError(f"{values.as_posix()}: the chart's values file is required")

        return self

    @property
    def declared_charts(self) -> dict[str, str]:
        return self.k8s.charts if self.k8s is not None else {}

    def compose_bundles(self) -> list[ComposeBundle]:
        # Local bundles (group options and bare entries) in declaration order.
        return [
            ComposeBundle(
                name,
                self.root / "stack/generated/compose" / f"compose.{name}.yaml",
                list(entry.workloads),
                dict(entry.vars),
            )
            for name, entry in chain.from_iterable(
                entry.options.items() if isinstance(entry, ServiceGroup) else ((name, entry),)
                for name, entry in self.services.items()
            )
            if not entry.remote
        ]

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
        # Reject name collisions between entries and options.
        entry_names = set(services)
        claimed: set[str] = set()
        for name, entry in services.items():
            if isinstance(entry, ServiceGroup):
                for option_name in entry.options:
                    if option_name in entry_names or option_name in claimed:
                        raise ValueError(
                            f"services.{name}.options.{option_name}: {option_name} is already an entry or option name"
                        )
                    claimed.add(option_name)

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
        if self.ref is not None and re.fullmatch(rf"oci://\S+@{SHA256_DIGEST_REGEX}", self.ref) is None:
            raise ValueError(f"ref {self.ref!r} must be a digest-pinned OCI include (oci://…@sha256:…)")
        if self.ref is not None and self.workloads:
            raise ValueError("an entry declares ref: or workloads:, not both")
        if self.ref is None and not self.workloads:
            raise ValueError("ref: or workloads: is required")
        if self.ref is not None and self.vars:
            raise ValueError("vars is not allowed with ref:")
        for name in self.workloads:
            if not name:
                raise ValueError("workload names must be non-empty")
        return self


def _service_member_kind(value: object) -> str:
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
        for name, reference in self.charts.items():
            if not name:
                raise ValueError("charts: names must be non-empty")
            if not reference.startswith(CHART_REFERENCE_PREFIX):
                raise ValueError(
                    f"charts.{name}: {reference!r} must be an upstream-native OCI reference "
                    f"({CHART_REFERENCE_PREFIX}host/path:tag or {CHART_REFERENCE_PREFIX}host/path@digest)"
                )
            remainder = reference.removeprefix(CHART_REFERENCE_PREFIX)
            match = DECLARED_REFERENCE_PATTERN.fullmatch(remainder)
            if match is None or not (match.group("tag") or match.group("digest")):
                raise ValueError(f"charts.{name}: {reference!r} must carry a tag or a digest")
        if not (self.workloads or self.charts):
            raise ValueError("the k8s section must declare workloads or charts")
        return self


class WorkloadMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: object = None
    annotations: dict[str, object] = Field(default_factory=dict)


class WorkloadDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    metadata: WorkloadMetadata = Field(default_factory=WorkloadMetadata)

    @property
    def dev_toggles(self) -> tuple[object | None, object]:
        return self.metadata.annotations.get(DEV_TOGGLES_ANNOTATION), self.metadata.name


class ComposeBundle(NamedTuple):
    name: str
    output: Path
    workloads: list[str]
    vars: dict[str, str]


def require_compose_stack(config: Config | None) -> Config:
    if config is None or not config.services:
        raise RuntimeError("docker-devkit.yaml must exist and declare services")
    return config


def chart_lock_key(name: str) -> str:
    return name.upper().replace("-", "_") + "_CHART"


def parse_dev_toggles(where: str, raw: str) -> dict[str, str]:
    toggles: dict[str, str] = {}
    for item in raw.split(","):
        toggle_name, separator, default = item.strip().partition("=")
        if not separator:
            raise ValueError(f"{where}: {DEV_TOGGLES_ANNOTATION} entry {item.strip()!r} must be NAME=default")
        if ENV_NAME_PATTERN.fullmatch(toggle_name) is None:
            raise ValueError(f"{where}: {DEV_TOGGLES_ANNOTATION} name {toggle_name!r} must be ALL_CAPS")
        if not default:
            raise ValueError(f"{where}: {DEV_TOGGLES_ANNOTATION} default for {toggle_name} is empty")
        if toggle_name in toggles:
            raise ValueError(f"{where}: {DEV_TOGGLES_ANNOTATION} names {toggle_name} twice")
        toggles[toggle_name] = default
    return toggles


def mirrored_ref(reference: str, prefix: str) -> str:
    if reference.startswith(f"{prefix}/"):
        raise RuntimeError(f"declarations are upstream-native; {reference} already carries the mirror prefix {prefix}")
    return f"{prefix}/{reference}"


def is_mapping(value: object) -> TypeIs[dict[str, object]]:
    return isinstance(value, dict)


def is_list(value: object) -> TypeIs[list[object]]:
    return isinstance(value, list)
