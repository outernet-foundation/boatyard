from __future__ import annotations

import importlib.metadata
import re
import tomllib
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Annotated, Literal, NamedTuple, Self

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
import yaml

from boatyard.models.pyproject import PYPROJECT_PATH, PyprojectToml
from boatyard.models.image import BuiltBaseEntry, Image, load_images
from boatyard.temp import PLATFORM_PATTERN, SHA256_DIGEST_REGEX
from boatyard.yaml import Yaml, is_mapping, load_yaml_model

DEV_SENTINEL = "0.0.0.dev0"

DEFAULT_MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"

PYTHON_VERSION_PATH = Path(".python-version")

ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

PLATFORM_KEY = "platform"

# Match ${ALL_CAPS} placeholder slots in workload files.
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

# Match a compose brace slot with its preceding dollar run.
COMPOSE_SLOT = re.compile(r"(\$+)\{([A-Z][A-Z0-9_]*)(:[^{}]*)?\}")

# Match path:tag or path@digest.
DECLARED_REFERENCE_PATTERN = re.compile(
    rf"^(?P<path>[^:@\s$]+)(?::(?P<tag>[^@\s$]+))?(?:@(?P<digest>{SHA256_DIGEST_REGEX}))?$"
)

TEMPLATE_KEY_PATTERN = re.compile(r"\{([a-z][a-z0-9_]*)\}")

# Match the range operators a ref tag must never carry; exact tags only.
REF_TAG_OPERATOR_PATTERN = re.compile(r"[<>=!~^*]")

OCI_REFERENCE_PREFIX = "oci://"

AUTH_GROUP_NAME = "auth"
EDGE_SERVICE_NAME = "edge"
EDGE_LISTEN_PORT = 8443
BARRIER_SERVICE = "wait-for-resources"
CADDY_IMAGE = "CADDY_IMAGE"
ALPINE_IMAGE = "ALPINE_IMAGE"

DEFAULT_BARRIER_IMAGE = "docker.io/library/alpine:3.20"
DEFAULT_EDGE_IMAGE = "docker.io/library/caddy:2.8.4-alpine"

COMPOSE_OUTPUT_DIR = Path("stack/generated/compose")

Target = Literal["both", "compose", "k8s"]

Substrate = Literal["compose", "k8s"]

LOCK_SLOT_MARKER = "__BOATYARD_LOCK__"
TREE_SLOT_MARKER = "__BOATYARD_TREE__"

RESERVED_SLOT_MARKERS = (LOCK_SLOT_MARKER, TREE_SLOT_MARKER)

TREE_SENTINEL_PATTERN = re.compile(re.escape(TREE_SLOT_MARKER) + r"([A-Z][A-Z0-9_]*)__")


class ConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requires: str
    gateway: PortNumber | None = None
    repository: str | None = None
    base_images: dict[AllCapsName, str | BuiltBaseEntry] = Field(default_factory=dict)
    barrier_image: str = DEFAULT_BARRIER_IMAGE
    edge_image: str = DEFAULT_EDGE_IMAGE
    project_name: str
    platforms: list[str] = Field(default_factory=list)
    services: dict[str, ServiceMember] = Field(default_factory=dict)
    k8s: K8sConfig | None = None
    mirror_prefix: str = DEFAULT_MIRROR_PREFIX

    score_dir: Path = Path("stack/score")
    k8s_output: Path = Path("stack/generated/k8s/manifests.yaml")
    k8s_local_output: Path = Path("stack/generated/k8s/manifests.local.yaml")
    score_compose_version: str = "0.42.0"
    score_k8s_version: str = "0.15.0"
    helm_version: str = "3.22.1"

    images: dict[str, Image] = Field(default_factory=dict, exclude=True)
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    @classmethod
    def load(cls, path: Path) -> ConfigFile:
        if "${" in path.read_text(encoding="utf-8"):
            raise ValueError(f"{path.as_posix()}: boatyard.yml carries no ${{...}} spelling")
        config = load_yaml_model(cls, path)
        _derive(config, path.parent)
        return config

    @property
    def declared_charts(self) -> dict[str, str]:
        return self.k8s.charts if self.k8s is not None else {}

    def local_service_entries(self) -> list[tuple[str, ServiceEntry]]:
        return [(row.name, row.entry) for row in flatten_service_members(self.services) if not row.entry.remote]

    def remote_declared_entries(self) -> list[tuple[str, str, ServiceEntry]]:
        return [
            (row.name, row.entry.ref.removeprefix(OCI_REFERENCE_PREFIX), row.entry)
            for row in flatten_service_members(self.services)
            if row.entry.ref is not None
        ]

    @property
    def pulled_base_images(self) -> dict[str, str]:
        return {name: reference for name, reference in self.base_images.items() if isinstance(reference, str)}

    @property
    def owned_images(self) -> dict[str, str]:
        # Boatyard-emitted service pins: the barrier rides local entries, the edge a gateway
        owned: dict[str, str] = {}
        if self.local_service_entries():
            owned[ALPINE_IMAGE] = self.barrier_image
        if self.gateway is not None:
            owned[CADDY_IMAGE] = self.edge_image
        return owned

    @property
    def tree_sentinels(self) -> dict[str, str]:
        # The compose barrier pre-images over the build-set slot supply; TREE_SENTINEL_PATTERN finds them
        return {slot: f"{TREE_SLOT_MARKER}{slot}__" for slot in (image.sha_env for image in self.images.values())}

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
    def _validate_base_images(cls, value: dict[AllCapsName, str | BuiltBaseEntry]) -> dict[AllCapsName, str | BuiltBaseEntry]:
        for name, reference in value.items():
            if name in (ALPINE_IMAGE, CADDY_IMAGE):
                raise ValueError(
                    f"base_images.{name}: the barrier and edge pins are boatyard-owned (barrier_image:/edge_image:)"
                )
            if isinstance(reference, str):
                _require_declared_reference(reference, f"base_images.{name}")
        return value

    @field_validator("barrier_image", "edge_image")
    @classmethod
    def _validate_owned_image_pins(cls, value: str, info: ValidationInfo) -> str:
        _require_declared_reference(value, str(info.field_name))
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
        for row in flatten_service_members(services):
            if row.name in vocabulary:
                raise ValueError(f"{row.site}: {row.name} is already claimed at {vocabulary[row.name]}")
            vocabulary[row.name] = row.site

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
                raise ValueError(f"{where}: route path {route.path} is already declared at {owner[route.path]}")
            owner[route.path] = where

        if AUTH_GROUP_NAME in services:
            auth = services[AUTH_GROUP_NAME]
            if not isinstance(auth, ServiceGroup):
                raise ValueError(f"services.{AUTH_GROUP_NAME} must be a group (the name is reserved)")
            for option_name, option in auth.options.items():
                if len(option.routes) != 1:
                    raise ValueError(
                        f"services.{AUTH_GROUP_NAME}.options.{option_name}: an auth option declares exactly one route"
                    )
        return services

    @model_validator(mode="after")
    def _validate(self) -> Self:
        # Compose is unconditional; k8s is the optional substrate.
        if not self.services:
            raise ValueError("services is required")
        any_local = any(not row.entry.remote for row in flatten_service_members(self.services))
        if any_local and not self.repository:
            raise ValueError("repository is required by the local entries (workloads:)")
        if (self.base_images or self.owned_images) and not self.platforms:
            raise ValueError(
                "platforms is required when images are declared (an amd64-only repo declares [linux/amd64])"
            )

        # A var naming a declared axis carries a declared variant of it.
        axes = variant_axes(self.services)
        for row in flatten_service_members(self.services):
            for name, value in row.entry.vars.items():
                if name in axes and value not in axes[name]:
                    raise ValueError(
                        f"{row.site}: vars: {name}={value!r} names no declared variant of {name} ({axes[name]})"
                    )
        if self.k8s is not None:
            for name, value in self.k8s.vars.items():
                if name in axes and value not in axes[name]:
                    raise ValueError(f"k8s.vars: {name}={value!r} names no declared variant of {name} ({axes[name]})")
        return self


def _require_all_caps_name(value: str) -> str:
    if ENV_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{value!r} must be an ALL_CAPS env name")
    return value


AllCapsName = Annotated[str, AfterValidator(_require_all_caps_name)]


def _require_port_range(value: int) -> int:
    if not 1 <= value <= 65535:
        raise ValueError(f"{value} is outside the port range 1-65535")
    return value


PortNumber = Annotated[int, Field(strict=True), AfterValidator(_require_port_range)]


def _require_nonempty_values(values: dict[str, str]) -> dict[str, str]:
    empty = sorted(name for name, value in values.items() if not value)
    if empty:
        raise ValueError(f"vars values must be non-empty: {', '.join(empty)}")
    return values


VarsMap = Annotated[dict[str, str], AfterValidator(_require_nonempty_values)]


class ServiceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: dict[str, ServiceEntry] = Field(default_factory=dict)
    axes: dict[AllCapsName, list[str]] = Field(default_factory=dict)
    workloads: list[str] = Field(default_factory=list)
    routes: list[RouteConfig] = Field(default_factory=list)
    ports: list[PortDeclaration] = Field(default_factory=list)

    @field_validator("axes")
    @classmethod
    def _validate_axes(cls, value: dict[AllCapsName, list[str]]) -> dict[AllCapsName, list[str]]:
        if len(value) != 1:
            raise ValueError("axes: declares exactly one axis (a cross-product of axes is refused)")
        for axis, values in value.items():
            if not values:
                raise ValueError(f"axes.{axis}: must declare at least one value")
            seen: set[str] = set()
            for variant in values:
                if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", variant) is None:
                    raise ValueError(f"axes.{axis}: value {variant!r} must be a bare lowercase spelling like cuda")
                if variant in seen:
                    raise ValueError(f"axes.{axis}: value {variant!r} is declared twice")
                seen.add(variant)
        return value

    @model_validator(mode="after")
    def _expand_variant_options(self) -> Self:
        if bool(self.options) == bool(self.axes):
            raise ValueError("a group declares exactly one of options: or axes:")
        if self.axes:
            if not self.workloads:
                raise ValueError("an axes group carries its workloads at the group level")
            [axis, values] = next(iter(self.axes.items()))
            self.options = {
                variant: ServiceEntry(workloads=list(self.workloads), vars={axis: variant}) for variant in values
            }
        elif self.workloads:
            raise ValueError("group-level workloads: ride only with axes:")
        return self


class ServiceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str | None = None
    workloads: list[str] = Field(default_factory=list)
    vars: VarsMap = Field(default_factory=dict)
    routes: list[RouteConfig] = Field(default_factory=list)
    ports: list[PortDeclaration] = Field(default_factory=list)

    @property
    def remote(self) -> bool:
        return self.ref is not None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.ref is not None:
            if not self.ref.startswith(OCI_REFERENCE_PREFIX):
                raise ValueError(f"ref {self.ref!r} must be an OCI include ({OCI_REFERENCE_PREFIX}host/path:tag)")
            remainder = self.ref.removeprefix(OCI_REFERENCE_PREFIX)
            if "@" in remainder:
                raise ValueError(f"ref {self.ref!r} must carry a published tag, not a digest")
            reference, separator, tag = remainder.rpartition(":")
            if not separator or not reference or not tag or re.search(r"\s", remainder):
                raise ValueError(f"ref {self.ref!r} must carry a tag ({OCI_REFERENCE_PREFIX}host/path:tag)")
            if tag == "latest":
                raise ValueError(f"ref {self.ref!r} must not carry the mutable tag 'latest'")
            if REF_TAG_OPERATOR_PATTERN.search(tag) or any(part in ("x", "X") for part in tag.split(".")):
                raise ValueError(f"ref {self.ref!r}: the tag {tag!r} must be an exact tag, not a range")
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
    return "group" if is_mapping(value) and ("options" in value or "axes" in value) else "entry"


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
    published: PortNumber
    target: PortNumber
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str | None = None


class K8sConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workloads: list[str] = Field(default_factory=list)
    vars: VarsMap = Field(default_factory=dict)
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
            _require_declared_reference(reference.removeprefix(OCI_REFERENCE_PREFIX), f"charts.{name}")
        if not (self.workloads or self.charts):
            raise ValueError("the k8s section must declare workloads or charts")
        return self


class Slot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = True


class FlattenedServiceEntry(NamedTuple):
    root: str
    option: str | None
    name: str
    site: str
    entry: ServiceEntry


class ResolvedService(NamedTuple):
    name: str
    entry: ServiceEntry
    routes: list[RouteConfig]
    ports: list[PortDeclaration]
    auth: bool = False


def _derive(config: ConfigFile, root: Path) -> None:
    # The load-time derivation over the parsed file: toolchain templates, the Dockerfile build
    # set, the file-reference checks, and the per-entry consumer surfaces.
    installed = importlib.metadata.version("boatyard")
    if installed != DEV_SENTINEL and not SpecifierSet(config.requires).contains(installed, prereleases=True):
        raise ValueError(f"boatyard {installed} does not satisfy requires={config.requires!r}")
    config.root = root

    # Resolve {uv} and {python} templates in the pulled base_images against the toolchain pins.
    pins: dict[str, str] = {}
    pulled = config.pulled_base_images
    used = sorted({key for reference in pulled.values() for key in TEMPLATE_KEY_PATTERN.findall(reference)})
    for key in used:
        if key == "uv":
            pyproject = root / PYPROJECT_PATH
            pyproject_document = PyprojectToml.model_validate(
                tomllib.loads(pyproject.read_text(encoding="utf-8")) if pyproject.is_file() else {}
            )
            pins[key] = pyproject_document.uv_version_pin()
        elif key == "python":
            python_version = root / PYTHON_VERSION_PATH
            if not python_version.is_file():
                raise ValueError(f"{PYTHON_VERSION_PATH.as_posix()}: the {{python}} template requires this file")
            # Take the first non-empty line.
            version = next(
                (line.strip() for line in python_version.read_text(encoding="utf-8").splitlines() if line.strip()),
                "",
            )
            if not version:
                raise ValueError(f"{PYTHON_VERSION_PATH.as_posix()}: the {{python}} template requires a non-empty file")
            pins[key] = version
        else:
            raise ValueError(f"base_images template key {{{key}}} is not one of the toolchain pins {{uv}}, {{python}}")
    config.base_images = {
        name: TEMPLATE_KEY_PATTERN.sub(partial(_resolve_template_key, pins), reference)
        if isinstance(reference, str)
        else reference
        for name, reference in config.base_images.items()
    }

    # Derive the image build set from the Dockerfile tree.
    images = load_images(root, config.base_images, config.owned_images)
    if images and config.repository is None:
        listed = ", ".join(sorted(images))
        raise ValueError(f"repository is required by the Dockerfile image directories {listed}")
    config.images = images

    # Enforce the lock pairing laws.
    declared_charts = config.declared_charts
    declares_routes = any(
        entry.routes or (isinstance(entry, ServiceGroup) and any(option.routes for option in entry.options.values()))
        for entry in config.services.values()
    )
    if declares_routes and config.gateway is None:
        raise ValueError("routes require a gateway declaration")
    for chart in declared_charts:
        values = root / config.score_dir / "k8s" / "values" / f"{chart}.yaml"
        if not values.is_file():
            raise ValueError(f"{values.as_posix()}: the chart's values file is required")

    # Declared inputs exist: every referenced workload file is on disk beside the config.
    for row in flatten_service_members(config.services):
        for workload_name in row.entry.workloads:
            workload_path = root / config.score_dir / workload_name
            if not workload_path.is_file():
                raise ValueError(f"{workload_path.as_posix()}: the workload file is required")


def variant_axes(services: dict[str, ServiceGroup | ServiceEntry]) -> dict[str, list[str]]:
    # The declared variant axes: one declaring group each, values from its axes map
    axes: dict[str, list[str]] = {}
    sites: dict[str, str] = {}
    for name, member in services.items():
        if not isinstance(member, ServiceGroup) or not member.axes:
            continue
        axis, values = next(iter(member.axes.items()))
        if axis in axes:
            raise ValueError(
                f"services.{name}.axes: {axis} is already declared at {sites[axis]} (one selection point per axis)"
            )
        axes[axis] = list(values)
        sites[axis] = f"services.{name}.axes"
    return axes


def flatten_service_members(services: dict[str, ServiceGroup | ServiceEntry]) -> list[FlattenedServiceEntry]:
    # Options ride under their qualified name; bare entries are their own root.
    flattened: list[FlattenedServiceEntry] = []
    for root_name, member in services.items():
        if isinstance(member, ServiceGroup):
            flattened.extend(
                FlattenedServiceEntry(
                    root=root_name,
                    option=option_name,
                    name=qualified_option_name(root_name, option_name),
                    site=f"services.{root_name}.options.{option_name}",
                    entry=option,
                )
                for option_name, option in member.options.items()
            )
        else:
            flattened.append(
                FlattenedServiceEntry(
                    root=root_name, option=None, name=root_name, site=f"services.{root_name}", entry=member
                )
            )
    return flattened


def require_workload_reference(name: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9._-]+\.yaml", name) is None:
        raise ValueError(f"workload reference {name!r} must be a flat .yaml file name")


def _require_declared_reference(value: str, site: str) -> None:
    match = DECLARED_REFERENCE_PATTERN.fullmatch(value)
    if match is None or not (match.group("tag") or match.group("digest")):
        raise ValueError(f"{site}: {value!r} must carry a tag or a digest")


def compose_bundle_output_path(root: Path, name: str) -> Path:
    return root / COMPOSE_OUTPUT_DIR / f"compose.{name}.yaml"


def qualified_option_name(group: str, option_name: str) -> str:
    return f"{group}-{option_name}"


def lock_key_prefix(name: str) -> str:
    return name.upper().replace("-", "_") + "_"


def env_lock_slot(entry_name: str, lock_key: str) -> str:
    # The marker reserves the fan spelling against values-file keys; the values parse refuses it
    return entry_name.upper().replace("-", "_") + LOCK_SLOT_MARKER + lock_key


def env_tree_slot(entry_name: str, sha_env_name: str) -> str:
    return entry_name.upper().replace("-", "_") + TREE_SLOT_MARKER + sha_env_name


def chart_lock_key(name: str) -> str:
    return lock_key_prefix(name) + "CHART"


def bundle_lock_key(name: str) -> str:
    return lock_key_prefix(name) + "BUNDLE"


def artifact_slots(texts: list[str]) -> dict[str, Slot]:
    # Artifact text: odd-$ slots are the consumer surface; marker-bearing fan spellings are
    # machine-filled and a :- default spells optional.
    slots: dict[str, Slot] = {}
    for text in texts:
        for match in COMPOSE_SLOT.finditer(text):
            if len(match.group(1)) % 2 != 1 or any(marker in match.group(2) for marker in RESERVED_SLOT_MARKERS):
                continue
            slots[match.group(2)] = Slot(required=not (match.group(3) or "").startswith(":-"))
    return slots


def refuse_unresolved_references(
    entry_name: str,
    routes: list[RouteConfig],
    ports: list[PortDeclaration],
    texts: list[str],
    prefix: str = "",
) -> set[str]:
    # Union the services keys across the compose texts; a document without a services mapping contributes none.
    service_names: set[str] = set()
    for text in texts:
        document = yaml.safe_load(text)
        services = document.get("services") if is_mapping(document) else None
        if is_mapping(services):
            service_names.update(services)
    references = {f"{prefix}{route.upstream.partition(':')[0]}": route.path for route in routes}
    references.update({f"{prefix}{port.service}": str(port.published) for port in ports})
    unknown = sorted(name for name in references if name not in service_names)
    if unknown:
        name = unknown[0]
        raise ValueError(
            f"services entry {entry_name!r}: {references[name]!r} references {name!r}, "
            f"which resolves to no service in the bundle (services: {sorted(service_names)})"
        )
    return service_names


def mirrored_ref(reference: str, prefix: str) -> str:
    if reference.startswith(f"{prefix}/"):
        raise ValueError(f"{reference} must not carry the mirror prefix {prefix} (declarations are upstream-native)")
    return f"{prefix}/{reference}"


def _resolve_template_key(pins: Mapping[str, str], match: re.Match[str]) -> str:
    return pins[match.group(1)]


def double_dollars(text: str) -> str:
    return text.replace("$", "$$")
