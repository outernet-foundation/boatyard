from __future__ import annotations

import importlib.metadata
import re
import tomllib
from collections.abc import Mapping
from functools import partial
from itertools import groupby
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
from boatyard.models.image import Image, LocalBaseEntry, dockerfile_stem, parse_dockerfile
from boatyard.models.workload import (
    ROUTE_PATH_PATTERN,
    ROUTE_UPSTREAM_PATTERN,
    LoadedWorkload,
)
from boatyard.temp import PLATFORM_PATTERN, SHA256_DIGEST_REGEX
from boatyard.yaml import Yaml, is_mapping, load_yaml_model

DEV_SENTINEL = "0.0.0.dev0"

DEFAULT_MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"

PYTHON_VERSION_PATH = Path(".python-version")

ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

ENTRY_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")

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
GATEWAY_SERVICE_NAME = "boatyard-gateway"
EDGE_LISTEN_PORT = 8443
BARRIER_SERVICE = "wait-for-resources"

# Boatyard machinery pins: moved by boatyard releases, never authored, locked, or mirrored
# by consumers; pulled by tag from upstream until boatyard hosts its own mirror.
BARRIER_IMAGE = "docker.io/library/alpine:3.20"
EDGE_IMAGE = "docker.io/library/caddy:2.8.4-alpine"
BARRIER_IMAGE_SLOT = "BOATYARD_BARRIER_IMAGE"

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
    gateway_port: PortNumber | None = None
    repository: str | None = None
    base_images: dict[AllCapsName, str | LocalBaseEntry] = Field(default_factory=dict)
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
    def service_entries(self) -> list[FlattenedServiceEntry]:
        # The flattened selection vocabulary; the load-time variant derivation populates the options first
        return flatten_service_entries(self.services)

    def local_service_entries(self) -> list[tuple[str, ServiceEntry]]:
        return [(row.name, row.entry) for row in self.service_entries if not row.entry.remote]

    def local_workload_names(self) -> list[str]:
        # The lexical local set: options' workloads, variant groups' group-level workloads, and
        # bare entries' workloads — complete before the variant derivation populates options
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
            for row in self.service_entries
            if row.entry.ref is not None
        ]

    @property
    def remote_base_images(self) -> dict[str, str]:
        return {name: reference for name, reference in self.base_images.items() if isinstance(reference, str)}

    @property
    def variant_axes(self) -> dict[str, list[str]]:
        # The declared axes over the image set; equal axis names carry equal value sets by the
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
        cls, value: dict[AllCapsName, str | LocalBaseEntry]
    ) -> dict[AllCapsName, str | LocalBaseEntry]:
        for name, reference in value.items():
            if isinstance(reference, str):
                _require_declared_reference(reference, f"base_images.{name}")
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
        cls, services: dict[str, ServiceGroup | ServiceEntry], info: ValidationInfo
    ) -> dict[str, ServiceGroup | ServiceEntry]:
        # Entry names feed compose project names and the shared network name, so every
        # services key, options key, and project_name must match the one pattern.
        for name, member in services.items():
            if ENTRY_NAME_PATTERN.fullmatch(name) is None:
                raise ValueError(f"services.{name}: names match ^[a-z][a-z0-9-]*$")
            if isinstance(member, ServiceGroup):
                for option_name in member.options:
                    if ENTRY_NAME_PATTERN.fullmatch(option_name) is None:
                        raise ValueError(f"services.{name}.options.{option_name}: names match ^[a-z][a-z0-9-]*$")
        project_name = info.data.get("project_name")
        if isinstance(project_name, str) and ENTRY_NAME_PATTERN.fullmatch(project_name) is None:
            raise ValueError(f"project_name {project_name!r} must match ^[a-z][a-z0-9-]*$")
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
                if len(option.routes) > 1:
                    raise ValueError(
                        f"services.{AUTH_GROUP_NAME}.options.{option_name}: an auth option declares "
                        "at most one route; the exactly-one effective-route check runs at resolve "
                        "over authored plus adopted routes"
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
        if self.base_images and not self.platforms:
            raise ValueError(
                "platforms is required when images are declared (an amd64-only repo declares [linux/amd64])"
            )
        declares_routes = any(
            routes
            for member in self.services.values()
            for routes in (
                [member.routes, *(option.routes for option in member.options.values())]
                if isinstance(member, ServiceGroup)
                else [member.routes]
            )
        )
        if declares_routes and self.gateway_port is None:
            raise ValueError("routes require a gateway declaration")
        upstream_sites: list[tuple[str, str]] = [
            (f"base_images.{name}", reference) for name, reference in self.remote_base_images.items()
        ]
        if self.k8s is not None:
            upstream_sites.extend(
                (f"k8s.charts.{name}", reference.removeprefix(OCI_REFERENCE_PREFIX))
                for name, reference in self.k8s.charts.items()
            )
        for site, reference in upstream_sites:
            if reference.startswith(f"{self.mirror_prefix}/"):
                raise ValueError(
                    f"{site}: {reference} must not carry the mirror prefix {self.mirror_prefix} "
                    "(declarations are upstream-native)"
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
    routes: list[Route] = Field(default_factory=list)
    ports: list[PublishedPort] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_one_spelling(self) -> Self:
        # options: spells an authored group; group-level workloads: spells a variant group whose
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
    routes: list[Route] = Field(default_factory=list)
    ports: list[PublishedPort] = Field(default_factory=list)

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


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    upstream: str
    strip_prefix: bool = False
    raw: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if ROUTE_PATH_PATTERN.fullmatch(self.path) is None:
            raise ValueError(f"route path {self.path!r} must be a /-rooted segment chain, optionally wildcarded as …/*")
        if ROUTE_UPSTREAM_PATTERN.fullmatch(self.upstream) is None:
            raise ValueError(
                f"route upstream {self.upstream!r} must be a service name and a literal port (grafana:3000)"
            )
        if self.strip_prefix and not self.path.endswith("/*"):
            raise ValueError(f"route {self.path}: the path must end in /* for strip_prefix")
        return self


class PublishedPort(BaseModel):
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
    effective_routes: list[Route]
    effective_ports: list[PublishedPort]
    auth: bool


def _derive(config: ConfigFile, root: Path) -> None:
    # Derive the load-time facts
    installed = importlib.metadata.version("boatyard")
    if installed != DEV_SENTINEL and not SpecifierSet(config.requires).contains(installed, prereleases=True):
        raise ValueError(f"boatyard {installed} does not satisfy requires={config.requires!r}")
    config.root = root

    # Raise if a declared chart lacks its values file
    for chart in config.declared_charts:
        values = root / config.score_dir / "k8s" / "values" / f"{chart}.yaml"
        if not values.is_file():
            raise ValueError(f"the chart {chart} requires {values.as_posix()}")

    local_bases = {name: entry for name, entry in config.base_images.items() if isinstance(entry, LocalBaseEntry)}

    # Raise if one axis name carries two value sets
    declared_axes: dict[str, list[str]] = {}
    for key, entry in local_bases.items():
        for axis, values in entry.axis.items():
            if axis in declared_axes and declared_axes[axis] != values:
                raise ValueError(
                    f"base_images.{key}: the axis {axis} already carries the value set {declared_axes[axis]}"
                )
            declared_axes[axis] = values

    # Resolve the template keys against the toolchain pins
    remote_bases = config.remote_base_images
    pins = _toolchain_pins(root, remote_bases)
    for name, reference in config.base_images.items():
        if isinstance(reference, str):
            config.base_images[name] = TEMPLATE_KEY_PATTERN.sub(partial(_resolve_template_key, pins), reference)

    # Load each workload file exactly once
    score_dir = root / config.score_dir
    local_names = set(config.local_workload_names())
    k8s_names = set(config.k8s.workloads) if config.k8s is not None else set[str]()
    config.workloads = {
        name: LoadedWorkload.load(score_dir / name, remote_bases) for name in sorted(local_names | k8s_names)
    }

    # Derive the build set as the closure over declared edges
    declared = [
        path for workload in config.workloads.values() for path in workload.document.declared_dockerfiles(score_dir)
    ]
    edges = sorted([*declared, *(entry.dockerfile for entry in local_bases.values())], key=dockerfile_stem)
    by_name = {name: list(paths) for name, paths in groupby(edges, key=dockerfile_stem)}
    for name, paths in by_name.items():
        directories = sorted({(root / path).parent for path in paths})
        if len(directories) > 1:
            raise ValueError(f"the image name {name!r} is claimed from two directories: {directories}")
    facts = {name: parse_dockerfile(root / paths[0], root) for name, paths in by_name.items()}

    # Bind every declared axis to one ARG and one value set
    local_image_names = {key: dockerfile_stem(entry.dockerfile) for key, entry in local_bases.items()}
    entry_axes: dict[str, tuple[str, list[str]]] = {}
    for key, entry in local_bases.items():
        image_name = local_image_names[key]
        for axis, values in entry.axis.items():
            if axis not in facts[image_name].args:
                raise ValueError(f"base_images.{key}: axis {axis} names no ARG in {entry.dockerfile}")
            entry_axes[image_name] = (axis, values)

    # Resolve each FROM key into at most one local base edge
    resolvable = set(remote_bases) | set(local_bases)
    images: dict[str, Image] = {}
    for name, fact in facts.items():
        bases: dict[str, str] = {}
        for key in fact.base_keys:
            if key not in resolvable:
                raise ValueError(f"{fact.dockerfile}: FROM ${{{key}}} names no base_images key")
            if key in local_image_names:
                bases[key] = local_image_names[key]
        own_bases = set(bases.values())
        if name in local_image_names.values() and own_bases:
            raise ValueError(f"{fact.dockerfile}: the local base {min(own_bases)!r} is FROM'd by another local base")
        if len(own_bases) > 1:
            raise ValueError(f"{fact.dockerfile}: its FROMs name more than one local base {sorted(own_bases)}")
        base = next(iter(bases.values()), None)
        pair = entry_axes.get(name) or (entry_axes.get(base) if base is not None else None)
        axis, variants = (pair[0], list(pair[1])) if pair is not None else (None, [])
        images[name] = Image(
            name=fact.name,
            dockerfile=fact.dockerfile,
            bases=bases,
            axis=axis,
            variants=variants,
            sha_args=sorted({argument for argument in fact.args if argument.endswith("_SHA")}),
        )

    config.images = images

    # Publish one option per variant of the group's single axis
    for name, member in config.services.items():
        if not (isinstance(member, ServiceGroup) and member.workloads):
            continue
        variants_by_axis: dict[str, list[str]] = {}
        image_names = {
            image_name
            for workload_name in member.workloads
            for image_name in config.workloads[workload_name].document.dockerfile_stems
        }
        for image_name in image_names:
            image = config.images.get(image_name)
            if image is not None and image.axis is not None:
                variants_by_axis.setdefault(image.axis, list(image.variants))
        if not variants_by_axis:
            raise ValueError(f"services.{name}: the image closure of the group's workloads declares no axis")
        if len(variants_by_axis) > 1:
            raise ValueError(
                f"services.{name}: the image closure of the group's workloads declares more than one axis "
                f"{sorted(variants_by_axis)}"
            )
        [(axis, variants)] = variants_by_axis.items()
        member.options = {
            variant: ServiceEntry(workloads=list(member.workloads), vars={axis: variant}) for variant in variants
        }

    # Raise if no Dockerfile consumes a local entry
    consumed = {image_name for image in images.values() for image_name in image.bases.values()}
    for key, image_name in sorted(local_image_names.items()):
        if image_name not in consumed:
            raise ValueError(f"base_images.{key}: no Dockerfile in the build set FROMs ${{{key}}}")

    # Raise if a sha ARG names no image directory
    computable = {image.sha_env for image in images.values()}
    for image in images.values():
        unknown = [argument for argument in image.sha_args if argument not in computable]
        if unknown:
            raise ValueError(f"{image.dockerfile}: ARG {', '.join(unknown)} names no image directory")

    # Raise if declared images lack a repository
    if images and config.repository is None:
        listed = ", ".join(sorted(images))
        raise ValueError(f"repository is required by the declared Dockerfile images {listed}")

    # Raise if two service entries claim one name
    vocabulary: dict[str, str] = {}
    for row in config.service_entries:
        if row.name in vocabulary:
            raise ValueError(f"{row.site}: {row.name} is already claimed at {vocabulary[row.name]}")
        vocabulary[row.name] = row.site

    # Raise if vars name an undeclared variant
    axes = config.variant_axes
    var_sources = [(f"{row.site}.vars", row.entry.vars) for row in config.service_entries]
    if config.k8s is not None:
        var_sources.append(("k8s.vars", config.k8s.vars))
    for source, variables in var_sources:
        for name, value in variables.items():
            if name in axes and value not in axes[name]:
                raise ValueError(f"{source}: {name}={value!r} names no declared variant of {name}")


def _toolchain_pins(root: Path, remote_bases: dict[str, str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    used = sorted({key for reference in remote_bases.values() for key in TEMPLATE_KEY_PATTERN.findall(reference)})
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
                raise ValueError(f"the {{python}} template requires {PYTHON_VERSION_PATH.as_posix()}")
            # Take the first non-empty line
            version = next(
                (line.strip() for line in python_version.read_text(encoding="utf-8").splitlines() if line.strip()),
                "",
            )
            if not version:
                raise ValueError(f"{PYTHON_VERSION_PATH.as_posix()} names no version for the {{python}} template")
            pins[key] = version
        else:
            raise ValueError(f"the template key {{{key}}} is neither {{uv}} nor {{python}}")
    return pins


def flatten_service_entries(services: dict[str, ServiceGroup | ServiceEntry]) -> list[FlattenedServiceEntry]:
    # Options appear under their qualified name; bare entries are their own root. The effective
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
                    auth=root_name == AUTH_GROUP_NAME,
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
                    auth=root_name == AUTH_GROUP_NAME,
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
    # The marker reserves the slot spelling against values-file keys; the values parse refuses it
    return entry_name.upper().replace("-", "_") + LOCK_SLOT_MARKER + lock_key


def env_tree_slot(entry_name: str, sha_env_name: str) -> str:
    return entry_name.upper().replace("-", "_") + TREE_SLOT_MARKER + sha_env_name


def chart_lock_key(name: str) -> str:
    return lock_key_prefix(name) + "CHART"


def bundle_lock_key(name: str) -> str:
    return lock_key_prefix(name) + "BUNDLE"


def artifact_slots(texts: list[str]) -> dict[str, Slot]:
    # Artifact text: odd-$ slots are the consumer surface; marker-bearing slot spellings are
    # machine-filled and a :- default spells optional (compose takes the default for
    # set-to-empty and unset alike, so unfilled optionals ride as env-file empties).
    slots: dict[str, Slot] = {}
    for text in texts:
        for match in COMPOSE_SLOT.finditer(text):
            if len(match.group(1)) % 2 != 1 or any(marker in match.group(2) for marker in RESERVED_SLOT_MARKERS):
                continue
            slots[match.group(2)] = Slot(required=not (match.group(3) or "").startswith(":-"))
    return slots


def refuse_unresolved_references(
    entry_name: str,
    routes: list[Route],
    ports: list[PublishedPort],
    texts: list[str],
) -> set[str]:
    # Union the services keys across the compose texts; a document without a services mapping contributes none.
    service_names: set[str] = set()
    for text in texts:
        document = yaml.safe_load(text)
        services = document.get("services") if is_mapping(document) else None
        if is_mapping(services):
            service_names.update(services)
    references = {route.upstream.partition(":")[0]: route.path for route in routes}
    references.update({port.service: str(port.published) for port in ports})
    unknown = sorted(name for name in references if name not in service_names)
    if unknown:
        name = unknown[0]
        raise ValueError(
            f"services entry {entry_name!r}: {references[name]!r} references {name!r}, "
            f"which resolves to no service in the bundle (services: {sorted(service_names)})"
        )
    return service_names


def mirrored_ref(reference: str, prefix: str) -> str:
    return f"{prefix}/{reference}"


def _resolve_template_key(pins: Mapping[str, str], match: re.Match[str]) -> str:
    return pins[match.group(1)]


def double_dollars(text: str) -> str:
    return text.replace("$", "$$")
