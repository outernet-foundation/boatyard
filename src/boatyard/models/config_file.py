from __future__ import annotations

import importlib.metadata
import re
import tomllib
from collections.abc import Mapping
from functools import partial
from pathlib import Path, PurePosixPath
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
from boatyard.models.image import DOCKERFILE_NAME, BuiltBaseEntry, Image, parse_dockerfile
from boatyard.models.workload import LoadedWorkload, load_workload, select_provisioner_files
from boatyard.markers import declared_dockerfile_paths
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
    workloads: dict[str, LoadedWorkload] = Field(default_factory=dict, exclude=True)
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

    @property
    def members(self) -> list[FlattenedServiceEntry]:
        # The flattened selection vocabulary; fan-derived options ride after the load-time derivation
        return flatten_service_members(self.services)

    def local_service_entries(self) -> list[tuple[str, ServiceEntry]]:
        return [(row.name, row.entry) for row in self.members if not row.entry.remote]

    def local_workload_names(self) -> list[str]:
        # The lexical local set: options' workloads, fan groups' group-level workloads, and
        # bare entries' workloads — complete before the fan derivation populates options
        names: list[str] = []
        for member in self.services.values():
            if isinstance(member, ServiceGroup):
                names.extend(name for option in member.options.values() for name in option.workloads)
                names.extend(member.workloads)
            elif not member.remote:
                names.extend(member.workloads)
        return names

    def remote_declared_entries(self) -> list[tuple[str, str, ServiceEntry]]:
        return [
            (row.name, row.entry.ref.removeprefix(OCI_REFERENCE_PREFIX), row.entry)
            for row in self.members
            if row.entry.ref is not None
        ]

    @property
    def pulled_base_images(self) -> dict[str, str]:
        return {name: reference for name, reference in self.base_images.items() if isinstance(reference, str)}

    @property
    def owned_images(self) -> dict[str, str]:
        # Boatyard-emitted service pins: the barrier rides local workloads, the edge a gateway
        owned: dict[str, str] = {}
        if self.local_workload_names():
            owned[ALPINE_IMAGE] = self.barrier_image
        if self.gateway is not None:
            owned[CADDY_IMAGE] = self.edge_image
        return owned

    @property
    def variant_axes(self) -> dict[str, list[str]]:
        # The fanned axes over the image set; equal axis names carry equal value sets by the
        # axis-equality law at load
        return {image.axis: list(image.variants) for image in self.images.values() if image.axis is not None}

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
    def _validate_base_images(
        cls, value: dict[AllCapsName, str | BuiltBaseEntry]
    ) -> dict[AllCapsName, str | BuiltBaseEntry]:
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
        any_local = bool(self.local_workload_names())
        if any_local and not self.repository:
            raise ValueError("repository is required by the local entries (workloads:)")
        if (self.base_images or self.owned_images) and not self.platforms:
            raise ValueError(
                "platforms is required when images are declared (an amd64-only repo declares [linux/amd64])"
            )
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
    workloads: list[str] = Field(default_factory=list)
    routes: list[RouteConfig] = Field(default_factory=list)
    ports: list[PortDeclaration] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_one_spelling(self) -> Self:
        # options: spells an authored group; group-level workloads: spells a fan group whose
        # options the load-time derivation publishes from the image closure
        if bool(self.options) == bool(self.workloads):
            raise ValueError("a group declares exactly one of options: or workloads:")
        for name in self.workloads:
            require_workload_reference(name)
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
    return "group" if is_mapping(value) and ("options" in value or "workloads" in value) else "entry"


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
    effective_routes: list[RouteConfig]
    effective_ports: list[PortDeclaration]


class ResolvedService(NamedTuple):
    name: str
    entry: ServiceEntry
    routes: list[RouteConfig]
    ports: list[PortDeclaration]
    auth: bool = False


def _derive(config: ConfigFile, root: Path) -> None:
    # The load-time derivation over the parsed file: toolchain templates, the one workload
    # pass, the Dockerfile build set, the derived group fan, and the file-reference checks.
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

    # Every workload file loads once here — the one lens serves the declared edges, the
    # derived fan, and both renderings; no later pass reloads a workload.
    score_dir = root / config.score_dir
    local_names = set(config.local_workload_names())
    k8s_names = set(config.k8s.workloads) if config.k8s is not None else set()
    config.workloads = {name: load_workload(score_dir, name) for name in sorted(local_names | k8s_names)}
    declared: list[str] = []
    for workload in config.workloads.values():
        for container, (kind, value) in workload.document.container_sources().items():
            if kind == "dockerfile":
                declared.append(value)
            elif value not in config.pulled_base_images:
                raise ValueError(
                    f"{workload.file_name}: the container {container!r} locks {value!r}, "
                    "which names no pulled base_images key"
                )

    selections = (
        (rendering, [workload for workload in config.workloads.values() if workload.file_name in names])
        for rendering, names in (("compose", local_names), ("k8s", k8s_names))
    )
    for rendering, selected_workloads in selections:
        for _name, entries in select_provisioner_files(score_dir, rendering, selected_workloads):
            declared.extend(path for entry in entries if is_mapping(entry) for path in declared_dockerfile_paths(entry))

    # The build set is the closure over declared edges: built entries, workload source
    # annotations, and provisioner markers. Nothing walks the directory tree.
    built: dict[str, str] = {}
    for name, entry in config.base_images.items():
        if not isinstance(entry, BuiltBaseEntry):
            continue
        path = root / entry.dockerfile
        if not path.is_file():
            raise ValueError(f"base_images.{name}: {entry.dockerfile} does not exist")
        built[name] = PurePosixPath(entry.dockerfile).parent.name

    declared_paths = sorted(
        set(declared) | {entry.dockerfile for entry in config.base_images.values() if isinstance(entry, BuiltBaseEntry)}
    )
    directories: dict[str, Path] = {}
    sites: dict[str, str] = {}
    for dockerfile in declared_paths:
        if dockerfile.startswith("/") or ".." in PurePosixPath(dockerfile).parts:
            raise ValueError(f"{dockerfile}: a declared dockerfile path must be repo-relative")
        directory = (root / dockerfile).parent
        stem = PurePosixPath(dockerfile).parent.name
        if stem not in directories:
            directories[stem] = directory
            sites[stem] = dockerfile
        elif directories[stem] != directory:
            raise ValueError(f"{dockerfile}: the image stem {stem!r} is already claimed at {sites[stem]}")
    for stem, directory in directories.items():
        if not (directory / DOCKERFILE_NAME).is_file():
            raise ValueError(f"{sites[stem]}: the declared dockerfile does not exist")

    facts = {name: parse_dockerfile(directory, root) for name, directory in sorted(directories.items())}

    # Every FROM key resolves against the declared vocabulary; built keys become base edges.
    resolvable = set(config.base_images) | set(config.owned_images) | set(built)
    bases: dict[str, dict[str, str]] = {}
    for name, fact in facts.items():
        bases[name] = {}
        for key in fact.base_keys:
            if key not in resolvable:
                raise ValueError(f"{fact.dockerfile}: FROM ${{{key}}} names no base_images key")
            if key in built:
                bases[name][key] = built[key]

    # One-hop grammar: a built base FROMs pulled entries only; a Dockerfile FROMs at most one built base.
    for name, fact in facts.items():
        own_bases = set(bases[name].values())
        if name in built.values() and own_bases:
            raise ValueError(f"{fact.dockerfile}: a built base must not FROM the built base {min(own_bases)!r}")
        if len(own_bases) > 1:
            raise ValueError(f"{fact.dockerfile}: FROMs more than one built base {sorted(own_bases)}")

    # A declared entry no Dockerfile consumes is dead.
    consumed = {stem for own_bases in bases.values() for stem in own_bases.values()}
    for name, stem in sorted(built.items()):
        if stem not in consumed:
            raise ValueError(f"base_images.{name}: no Dockerfile FROMs ${{{name}}} (dead declaration)")

    # The declared fan: the axis names an ARG in the built base's own Dockerfile; equal axis
    # names carry equal value sets across entries.
    axis_values: dict[str, list[str]] = {}
    axis_sites: dict[str, str] = {}
    entry_axes: dict[str, tuple[str, list[str]]] = {}
    for name, entry in config.base_images.items():
        if not isinstance(entry, BuiltBaseEntry):
            continue
        stem = built[name]
        for axis, values in entry.axis.items():
            if axis not in facts[stem].args:
                raise ValueError(f"base_images.{name}: axis {axis} names no ARG in {entry.dockerfile}")
            if axis in axis_values and axis_values[axis] != values:
                raise ValueError(
                    f"base_images.{name}: axis {axis} declares {values}; "
                    f"{axis_sites[axis]} already declares {axis_values[axis]}"
                )
            axis_values[axis] = values
            axis_sites[axis] = name
            entry_axes[stem] = (axis, list(values))

    images: dict[str, Image] = {}
    for name, fact in facts.items():
        axis: str | None = None
        variants: list[str] = []
        if name in entry_axes:
            axis, variants = entry_axes[name][0], list(entry_axes[name][1])
        else:
            [stem, *_] = [*bases[name].values(), None]
            if stem is not None and stem in entry_axes:
                axis, variants = entry_axes[stem][0], list(entry_axes[stem][1])
        images[name] = Image(
            name=fact.name,
            dockerfile=fact.dockerfile,
            bases=bases[name],
            axis=axis,
            variants=variants,
            sha_args=sorted({argument for argument in fact.args if argument.endswith("_SHA")}),
        )

    computable = {image.sha_env for image in images.values()}
    for image in images.values():
        unknown = [argument for argument in image.sha_args if argument not in computable]
        if unknown:
            raise ValueError(f"{image.dockerfile}: ARG {', '.join(unknown)} names no image directory")

    if images and config.repository is None:
        listed = ", ".join(sorted(images))
        raise ValueError(f"repository is required by the declared Dockerfile images {listed}")
    config.images = images

    # The derived fan: a fan group publishes one option per value of the single axis its
    # workloads' image closure fans (the variants' declared home is the built base entry).
    for name, member in config.services.items():
        if not (isinstance(member, ServiceGroup) and member.workloads):
            continue
        fans: dict[str, list[str]] = {}
        for workload_name in member.workloads:
            for kind, value in config.workloads[workload_name].document.container_sources().values():
                if kind != "dockerfile":
                    continue
                image = config.images.get(PurePosixPath(value).parent.name)
                if image is not None and image.axis is not None:
                    fans.setdefault(image.axis, list(image.variants))
        if not fans:
            raise ValueError(
                f"services.{name}: the image closure of the group's workloads fans nothing "
                "(a non-fanning local set spells one authored option)"
            )
        if len(fans) > 1:
            raise ValueError(
                f"services.{name}: the image closure of the group's workloads fans more than one axis {sorted(fans)}"
            )
        [(axis, variants)] = fans.items()
        member.options = {
            variant: ServiceEntry(workloads=list(member.workloads), vars={axis: variant}) for variant in variants
        }

    # The derived option names join the selection vocabulary with no collision.
    vocabulary: dict[str, str] = {}
    for row in config.members:
        if row.name in vocabulary:
            raise ValueError(f"{row.site}: {row.name} is already claimed at {vocabulary[row.name]}")
        vocabulary[row.name] = row.site

    # A var naming a fanned axis carries a declared variant of it.
    axes = config.variant_axes
    for row in config.members:
        for name, value in row.entry.vars.items():
            if name in axes and value not in axes[name]:
                raise ValueError(
                    f"{row.site}: vars: {name}={value!r} names no declared variant of {name} ({axes[name]})"
                )
    if config.k8s is not None:
        for name, value in config.k8s.vars.items():
            if name in axes and value not in axes[name]:
                raise ValueError(f"k8s.vars: {name}={value!r} names no declared variant of {name} ({axes[name]})")

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


def flatten_service_members(services: dict[str, ServiceGroup | ServiceEntry]) -> list[FlattenedServiceEntry]:
    # Options ride under their qualified name; bare entries are their own root. The effective
    # surface is the entry's own declarations beside its group's.
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
                    effective_routes=option.routes + member.routes,
                    effective_ports=option.ports + member.ports,
                )
                for option_name, option in member.options.items()
            )
        else:
            flattened.append(
                FlattenedServiceEntry(
                    root=root_name,
                    option=None,
                    name=root_name,
                    site=f"services.{root_name}",
                    entry=member,
                    effective_routes=member.routes,
                    effective_ports=member.ports,
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
