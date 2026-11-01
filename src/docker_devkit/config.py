from __future__ import annotations

import importlib.metadata
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal, NamedTuple, Self, TypeIs

import pathspec
import yaml
from bashrun.bash import bash, bash_output
from packaging.specifiers import SpecifierSet
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from strictyaml import load as load_strict_yaml

DEFAULT_CONFIG_PATH = Path("docker-devkit.yaml")
DEV_SENTINEL = "0.0.0.dev0"

DEFAULT_MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"

# The lock pairs with the root config at the repo root: build keys declared
# (base_images or charts) => the committed lock is required.
LOCK_PATH = Path("docker-devkit.lock")
LEFTOVER_MANIFEST_PATH = Path("workloads/images.yml")
LEFTOVER_LOCK_PATH = Path("workloads/images.lock")

# The toolchain pin sources base_images templates resolve against.
PYPROJECT_PATH = Path("pyproject.toml")
PYTHON_VERSION_PATH = Path(".python-version")

ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
PLATFORM_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")
VARIANT_VALUE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

SHA256_DIGEST_REGEX = r"sha256:[a-f0-9]{64}"

# path:tag or path@digest — upstream-native, never a bare path or a ${…} hole.
DECLARED_REFERENCE_PATTERN = re.compile(
    rf"^(?P<path>[^:@\s$]+)(?::(?P<tag>[^@\s$]+))?(?:@(?P<digest>{SHA256_DIGEST_REGEX}))?$"
)

# base_images values may template the repo's toolchain pins: {uv} resolves from
# tool.uv.required-version (an exact == pin only — a range refuses loudly), {python}
# from .python-version. The vendor's tag grammar stays authored data in the string.
TEMPLATE_KEY_PATTERN = re.compile(r"\{([a-z][a-z0-9_]*)\}")
UV_EXACT_PIN_PATTERN = re.compile(r"^==(?P<version>[^,*\s]+)$")

CHART_REFERENCE_PREFIX = "oci://"

SCORE_DIR = Path("stack/score")
K8S_OUTPUT = Path("stack/generated/k8s/manifests.yaml")
K8S_LOCAL_OUTPUT = Path("stack/generated/k8s/manifests.local.yaml")
SCORE_COMPOSE_VERSION = "0.42.0"
SCORE_K8S_VERSION = "0.15.0"
HELM_VERSION = "3.22.1"

BUNDLE_OUTPUT_DIR = Path("stack/generated/compose")

# A route path is a /-rooted segment chain, optionally wildcarded by a trailing /*; the
# root catch-all is exactly /*.
ROUTE_PATH_PATTERN = re.compile(r"^(?:/\*|/(?:[^/\s*]+(?:/[^/\s*]+)*)?(?:/\*)?)$")
# An upstream is a compose service name and a literal port.
ROUTE_UPSTREAM_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*:[0-9]+$")
# A remote include is an OCI reference pinned by digest.
REMOTE_REF_PATTERN = re.compile(rf"^oci://\S+@{SHA256_DIGEST_REGEX}$")
# A compose env hole: ${NAME}, ${NAME:?err}, ${NAME:-default}.
ENV_HOLE_PATTERN = re.compile(r"^\$\{[A-Z][A-Z0-9_]*(?::[^{}]*)?\}$")
PORT_PATTERN = re.compile(r"^[0-9]+$")

AUTH_GROUP_NAME = "auth"
EDGE_SERVICE_NAME = "edge"
CADDY_IMAGE = "CADDY_IMAGE"
DEV_TOGGLES_ANNOTATION = "outernet.internal/dev-toggles"

DOCKERFILE_NAME = "Dockerfile"
FROM_LINE_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
ARG_LINE_PATTERN = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)")
COPY_FROM_LINE_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.IGNORECASE)
BARE_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
BUILD_ARG_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z0-9_]+\}$")

# The compose-edge platform selector, read from the merged shell-over-.env view (the
# COMPOSE_PROFILES precedence); never host introspection.
PLATFORM_VARIABLE = "PLATFORM"

type BakeValue = str | list[str] | dict[str, str]

Target = Literal["both", "compose", "k8s"]

PLAIN_VARIANT = "common"


def chart_lock_key(name: str) -> str:
    return name.upper().replace("-", "_") + "_CHART"


def parse_dev_toggles(where: str, raw: str) -> dict[str, str]:
    # The dev-toggles spelling law (NAME=default pairs): the one parser generation's
    # validator and up's dev overlay consume.
    toggles: dict[str, str] = {}
    for item in raw.split(","):
        toggle_name, separator, default = item.strip().partition("=")
        if not separator:
            raise ValueError(
                f"{where}: {DEV_TOGGLES_ANNOTATION} entry {item.strip()!r} misses its default; "
                "the ruled spelling is NAME=default pairs"
            )
        if ENV_NAME_PATTERN.fullmatch(toggle_name) is None:
            raise ValueError(f"{where}: {DEV_TOGGLES_ANNOTATION} name {toggle_name!r} must be ALL_CAPS")
        if not default:
            raise ValueError(f"{where}: {DEV_TOGGLES_ANNOTATION} default for {toggle_name} is empty")
        if toggle_name in toggles:
            raise ValueError(f"{where}: {DEV_TOGGLES_ANNOTATION} names {toggle_name} twice")
        toggles[toggle_name] = default
    return toggles


class ServiceBuild(BaseModel):
    # Derived by load_config from the Dockerfile tree (derive_services); never authored.
    model_config = ConfigDict(extra="forbid")

    name: str
    dockerfile: str
    contexts: list[str] = Field(default_factory=list)
    axis: str | None = None
    variants: list[str] = Field(default_factory=list)
    sha_args: list[str] = Field(default_factory=list)
    dev_stage: bool = False

    @model_validator(mode="after")
    def _axis_pairs_with_variants(self) -> Self:
        if (self.axis is None) != (not self.variants):
            raise ValueError("axis and variants declare together; an axis-bearing service carries its declared values")
        return self

    @property
    def target_variants(self) -> list[tuple[str, str | None]]:
        variants: list[str | None] = list(self.variants) or [None]
        return [(f"{self.name}-{variant}" if variant is not None else self.name, variant) for variant in variants]


class ParsedDockerfile(NamedTuple):
    # The pre-resolution parse of one service Dockerfile; the variant-axis graph
    # resolves over these records before any ServiceBuild is constructed.
    name: str
    dockerfile: str
    contexts: list[str]
    sha_args: list[str]
    dev_stage: bool
    axis_arg: str | None


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    upstream: str
    strip_prefix: bool = False
    raw: str | None = None

    @field_validator("path")
    @classmethod
    def _require_route_path(cls, path: str) -> str:
        if ROUTE_PATH_PATTERN.fullmatch(path) is None:
            raise ValueError(f"route path {path!r} must be a /-rooted segment chain, optionally wildcarded as …/*")
        return path

    @field_validator("upstream")
    @classmethod
    def _require_upstream(cls, upstream: str) -> str:
        if ROUTE_UPSTREAM_PATTERN.fullmatch(upstream) is None:
            raise ValueError(f"route upstream {upstream!r} must be a service name and a literal port (grafana:3000)")
        return upstream

    @model_validator(mode="after")
    def _strip_prefix_needs_a_wildcard(self) -> Self:
        if self.strip_prefix and not self.path.endswith("/*"):
            raise ValueError(f"route {self.path}: strip_prefix strips a wildcard prefix; the path must end in /*")
        return self


class PortDeclaration(BaseModel):
    # A port is either the mapping form or the string form published/target[/protocol],
    # which names the entry's or option's namesake service as its owner (the
    # _coerce_services pre-pass injects the service before validation).
    model_config = ConfigDict(extra="forbid")

    service: str
    published: str
    target: str
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str | None = None

    @field_validator("published", "target")
    @classmethod
    def _numeric_or_hole(cls, value: str, info: ValidationInfo) -> str:
        if PORT_PATTERN.fullmatch(value) is None and ENV_HOLE_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"ports {info.field_name} {value!r} must be a literal port number or a ${{…}} env hole "
                "(published and target may both be holes)"
            )
        return value


class ServiceEntry(BaseModel):
    # An option (inside a group's options:) or a bare riding entry. The source is either a
    # remote include (ref:) or a producer-local workload list (workloads:/vars:); routes and
    # ports attach to either shape.
    model_config = ConfigDict(extra="forbid")

    ref: str | None = None
    workloads: list[str] = Field(default_factory=list)
    vars: dict[str, str] = Field(default_factory=dict)
    routes: list[RouteConfig] = Field(default_factory=list)
    ports: list[PortDeclaration] = Field(default_factory=list)

    @property
    def remote(self) -> bool:
        return self.ref is not None

    @field_validator("ref")
    @classmethod
    def _require_digest_pinned_ref(cls, ref: str | None) -> str | None:
        if ref is not None and REMOTE_REF_PATTERN.fullmatch(ref) is None:
            raise ValueError(f"ref {ref!r} must be a digest-pinned OCI include (oci://…@sha256:…)")
        return ref

    @field_validator("workloads")
    @classmethod
    def _require_workloads(cls, workloads: list[str]) -> list[str]:
        if not workloads:
            raise ValueError("a local entry carries at least one workload; it renders and publishes an artifact")
        for name in workloads:
            if not name:
                raise ValueError("workload names must be non-empty")
        return workloads

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if self.ref is not None and self.workloads:
            raise ValueError("an entry is either ref: or workloads: — exactly one source, never both")
        if self.ref is None and not self.workloads:
            raise ValueError(
                "an entry is either ref: or workloads: — exactly one source, never neither (a group spells options:)"
            )
        if self.ref is not None and self.vars:
            raise ValueError("vars fill local workload renderings; a ref carries its producer's vars")
        return self


class ServiceGroup(BaseModel):
    # A selectable entry: mutually exclusive options picked through BUNDLES=, with
    # group-level routes and ports riding whichever option is selected.
    model_config = ConfigDict(extra="forbid")

    options: dict[str, ServiceEntry]
    routes: list[RouteConfig] = Field(default_factory=list)
    ports: list[PortDeclaration] = Field(default_factory=list)


class K8sConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workloads: list[str] = Field(default_factory=list)
    vars: dict[str, str] = Field(default_factory=dict)
    charts: dict[str, str] = Field(default_factory=dict)

    @property
    def rendering(self) -> bool:
        return bool(self.workloads or self.charts)

    @field_validator("charts")
    @classmethod
    def _require_upstream_native_refs(cls, charts: dict[str, str]) -> dict[str, str]:
        for name, reference in charts.items():
            if not name:
                raise ValueError("chart names must be non-empty")
            if not reference.startswith(CHART_REFERENCE_PREFIX):
                raise ValueError(
                    f"charts.{name}: {reference!r} must be an upstream-native OCI reference "
                    f"({CHART_REFERENCE_PREFIX}host/path:tag or {CHART_REFERENCE_PREFIX}host/path@digest)"
                )
            remainder = reference.removeprefix(CHART_REFERENCE_PREFIX)
            match = DECLARED_REFERENCE_PATTERN.fullmatch(remainder)
            if match is None or not (match.group("tag") or match.group("digest")):
                raise ValueError(
                    f"charts.{name}: {reference!r} must carry a tag or a digest; a bare path or a ${{…}} hole refuses"
                )
        return charts

    @model_validator(mode="after")
    def _require_a_rendering(self) -> Self:
        if not self.rendering:
            raise ValueError("the k8s section declares neither workloads nor charts; remove it")
        return self


class DockerDevkitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requires: str
    gateway: str | None = None
    repository: str | None = None
    base_images: dict[str, str] = Field(default_factory=dict)
    variants: dict[str, list[str]] = Field(default_factory=dict)
    platforms: list[str] = Field(default_factory=list)
    project_name: str | None = None
    services: dict[str, ServiceGroup | ServiceEntry] = Field(default_factory=dict)
    k8s: K8sConfig | None = None
    mirror_prefix: str = DEFAULT_MIRROR_PREFIX

    score_dir: Path = SCORE_DIR
    k8s_output: Path = K8S_OUTPUT
    k8s_local_output: Path = K8S_LOCAL_OUTPUT
    score_compose_version: str = SCORE_COMPOSE_VERSION
    score_k8s_version: str = SCORE_K8S_VERSION
    helm_version: str = HELM_VERSION

    # Derived by load_config from the Dockerfile tree (derive_services); never authored.
    service_builds: dict[str, ServiceBuild] = Field(default_factory=dict, exclude=True)
    # The repo root the config was loaded from; load-injected, never authored.
    root: Path | None = Field(default=None, exclude=True)

    def service_shas(self) -> dict[str, str]:
        # The tree-tag environment over the derived services; compute_service_shas stays a
        # module-level global, late-bound by name (the test monkeypatch seam).
        assert self.root is not None
        return compute_service_shas(self.root, self.service_builds)

    def lock_entries(self) -> ImageLock:
        # The lock read where absence is legal; required-lock callers parse directly.
        assert self.root is not None
        lock_path = self.root / LOCK_PATH
        return parse_image_lock(lock_path) if lock_path.is_file() else ImageLock.model_validate({})

    @property
    def declared_charts(self) -> dict[str, str]:
        return self.k8s.charts if self.k8s is not None else {}

    def select_platform(self, environment: Mapping[str, str]) -> str | None:
        # Declaration-driven platform selection: a singleton selects itself, a multi-platform
        # repo names PLATFORM= in .env (shell env overriding), and a set value outside the
        # declared set refuses. None answers only a repo declaring no platforms at all.
        selected = environment.get(PLATFORM_VARIABLE, "").strip()
        if selected:
            if selected not in self.platforms:
                raise RuntimeError(
                    f"{PLATFORM_VARIABLE}={selected!r} matches none of this repo's declared platforms {self.platforms}"
                )
            return selected
        if not self.platforms:
            return None
        if len(self.platforms) == 1:
            return self.platforms[0]
        raise RuntimeError(
            f"this repo declares the platforms {self.platforms}; name {PLATFORM_VARIABLE}= in .env "
            "to select the compose edge's platform"
        )

    def score_dir_path(self) -> Path:
        assert self.root is not None
        return self.root / self.score_dir

    def k8s_output_path(self, local: bool) -> Path:
        assert self.root is not None
        return self.root / (self.k8s_local_output if local else self.k8s_output)

    def bundle_output(self, name: str) -> Path:
        assert self.root is not None
        return self.root / BUNDLE_OUTPUT_DIR / f"compose.{name}.yaml"

    def chart_values_path(self, chart: str) -> Path:
        assert self.root is not None
        return self.root / self.score_dir / "k8s" / "values" / f"{chart}.yaml"

    def compose_bundles(self) -> list[ComposeBundle]:
        # The local renderables: every workload-bearing option and bare entry, in declaration
        # order. Remote refs never render here; consumers include them by digest.
        bundles: list[ComposeBundle] = []
        for name, entry in self.services.items():
            if isinstance(entry, ServiceGroup):
                for option_name, option in entry.options.items():
                    if option.remote:
                        continue
                    bundles.append(
                        ComposeBundle(
                            option_name, self.bundle_output(option_name), list(option.workloads), dict(option.vars)
                        )
                    )
            elif not entry.remote:
                bundles.append(ComposeBundle(name, self.bundle_output(name), list(entry.workloads), dict(entry.vars)))
        return bundles

    def render_bake(self, *, push: bool, host: str, destination: str | None = None, dev: bool = False) -> str:
        # The per-run temp bake file: bake validates its own input by failing on it, so no
        # document model sits behind the render. Identical configs render byte-identical
        # files. Tags and cache refs root under the destination when one is given, else
        # the config's repository — one law for both push destinations.
        namespace = destination if destination is not None else self.repository
        services: dict[str, dict[str, dict[str, BakeValue]]] = {}
        for service in self.service_builds.values():
            axis = service.axis
            for target, variant in service.target_variants:
                args: dict[str, str] = {key: f"${{{key}:-}}" for key in self.base_images}
                if axis is not None and variant is not None:
                    args[axis] = variant
                args.update({sha: f"${{{sha}:?err}}" for sha in service.sha_args})
                build: dict[str, BakeValue] = {"dockerfile": service.dockerfile}
                if dev:
                    build["target"] = "dev"
                if self.platforms:
                    # A --load run writes a single arch, so a multi-arch declaration collapses
                    # to the host; a single declared platform rides through in both modes.
                    build["platforms"] = [host] if not push and len(self.platforms) > 1 else list(self.platforms)
                build["args"] = args
                if dev:
                    # The dev tag is per service, not per target: whichever cell a host
                    # builds, the overlay references one {repository}/{service}:dev image.
                    build["tags"] = [f"{namespace}/{service.name}:dev"]
                else:
                    build["tags"] = [
                        f"{namespace}/{target}:latest",
                        f"{namespace}/{target}:${{{service_sha_env(service.name)}}}",
                    ]
                if service.contexts:
                    build["additional_contexts"] = {
                        context: f"target:{context}-{variant}" if variant is not None else f"target:{context}"
                        for context in service.contexts
                    }
                if push:
                    cache = f"{namespace}/build-cache"
                    build["cache_to"] = [
                        f"type=registry,ref={cache}:{target},mode=max,image-manifest=true,oci-mediatypes=true"
                    ]
                    build["cache_from"] = [f"type=registry,ref={cache}:{target}"]
                services[target] = {"build": build}
        return yaml.safe_dump({"services": services}, sort_keys=False)

    def tool_requirements(self, target: Target) -> list[ToolRequirement]:
        # Fetch release binaries rather than go install, since score-spec tags are not valid
        # Go module versions.
        score_tools: list[tuple[str, str]] = []
        if target in ("both", "compose"):
            score_tools.append(("score-compose", self.score_compose_version))
        if target in ("both", "k8s") and self.k8s is not None:
            score_tools.append(("score-k8s", self.score_k8s_version))
        requirements = [
            ToolRequirement(
                binary=binary,
                version=version,
                release=f"https://github.com/score-spec/{binary}/releases/tag/{version}",
                archive=f"https://github.com/score-spec/{binary}/releases/download/{version}/{binary}_{version}_linux_amd64.tar.gz",
                extract=binary,
            )
            for binary, version in score_tools
        ]
        # Strip one path component for helm, whose archive nests its binary under linux-amd64/.
        if self.k8s is not None and self.k8s.charts and target in ("both", "k8s"):
            version = self.helm_version
            requirements.append(
                ToolRequirement(
                    binary="helm",
                    version=version,
                    release=f"https://github.com/helm/helm/releases/tag/v{version}",
                    archive=f"https://get.helm.sh/helm-v{version}-linux-amd64.tar.gz",
                    extract="--strip-components=1 linux-amd64/helm",
                )
            )
        return requirements

    def build_partitions(self) -> list[dict[str, str]]:
        # The CI fanout partitions: a union-find over the derived context edges groups
        # dependency closures, and load-time coherence guarantees one variant set per
        # closure, so one partition builds a whole closure group in a single bake
        # invocation; one partition per closure × variant cell. The cell schema
        # (targets, variant) is a named contract with the toolkit binding.
        services = self.service_builds
        parent = {service: service for service in services}

        for name, service in services.items():
            for dependency in service.contexts:
                left, right = _find(parent, name), _find(parent, dependency)
                if left != right:
                    parent[right] = left

        members: dict[str, list[str]] = {}
        for service in services:
            members.setdefault(_find(parent, service), []).append(service)

        entries_by_variant: dict[tuple[str | None, str], list[str]] = {}
        for group in sorted(sorted(member_list) for member_list in members.values()):
            for name in group:
                for target, variant in services[name].target_variants:
                    cell = variant if variant is not None else PLAIN_VARIANT
                    entries_by_variant.setdefault((services[name].axis, cell), []).append(target)

        return [
            {"targets": " ".join(sorted(targets)), "variant": cell}
            for (_axis, cell), targets in sorted(entries_by_variant.items(), key=lambda item: item[0][1])
        ]

    def _route_sites(self) -> list[tuple[str, RouteConfig]]:
        # Every route with its declaration site, in services declaration order — a group's
        # options first, then its own riding routes.
        return [
            (where, route)
            for name, entry in self.services.items()
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

    def _any_local_entries(self) -> bool:
        return any(
            not option.remote
            for entry in self.services.values()
            if isinstance(entry, ServiceGroup)
            for option in entry.options.values()
        ) or any(not entry.remote for entry in self.services.values() if not isinstance(entry, ServiceGroup))

    @field_validator("requires")
    @classmethod
    def _validate_requires(cls, value: str) -> str:
        if not value:
            raise ValueError("requires must be a non-empty PEP 440 specifier; use '>=0.0' for no constraint")
        SpecifierSet(value)
        return value

    @field_validator("gateway")
    @classmethod
    def _require_port_or_hole(cls, gateway: str | None) -> str | None:
        if gateway is None:
            return gateway
        if PORT_PATTERN.fullmatch(gateway) is None and ENV_HOLE_PATTERN.fullmatch(gateway) is None:
            raise ValueError(f"gateway {gateway!r} must be a literal port number or a ${{…}} env hole")
        return gateway

    @field_validator("repository")
    @classmethod
    def _require_registry_repository(cls, repository: str | None) -> str | None:
        if repository is None:
            return repository
        host, separator, path = repository.partition("/")
        if not separator or not host or not path or repository.endswith("/"):
            raise ValueError(
                "repository must be registry-host/repository-path with no trailing slash (ghcr.io/org/repo)"
            )
        return repository

    @field_validator("base_images")
    @classmethod
    def _require_tag_or_digest(cls, base_images: dict[str, str]) -> dict[str, str]:
        for name, reference in base_images.items():
            if ENV_NAME_PATTERN.fullmatch(name) is None:
                raise ValueError(f"base_images key {name!r} must be an ALL_CAPS env name; it becomes a build arg")
            match = DECLARED_REFERENCE_PATTERN.fullmatch(reference)
            if match is None or not (match.group("tag") or match.group("digest")):
                raise ValueError(f"base_images.{name}: {reference!r} must be 'path:tag' or 'path@digest'")
        return base_images

    @field_validator("variants")
    @classmethod
    def _require_declared_axes(cls, variants: dict[str, list[str]]) -> dict[str, list[str]]:
        # Value sets and defaults are declared, never probed: the first value is the
        # default cell, and the values ride target names and --targets cells.
        for axis, values in variants.items():
            if ENV_NAME_PATTERN.fullmatch(axis) is None:
                raise ValueError(f"variants key {axis!r} must be an ALL_CAPS ARG name; it names the receiver ARG")
            if not values:
                raise ValueError(
                    f"variants.{axis}: an axis declares at least one value; the first value is the default cell"
                )
            seen: set[str] = set()
            for value in values:
                if VARIANT_VALUE_PATTERN.fullmatch(value) is None:
                    raise ValueError(
                        f"variants.{axis}: value {value!r} must be a bare lowercase spelling like cuda; it rides target names"
                    )
                if value in seen:
                    raise ValueError(f"variants.{axis}: value {value!r} is declared twice")
                seen.add(value)
        return variants

    @field_validator("platforms")
    @classmethod
    def _require_arch_pairs(cls, platforms: list[str]) -> list[str]:
        for platform in platforms:
            if PLATFORM_PATTERN.fullmatch(platform) is None:
                raise ValueError(f"platforms entry {platform!r} must be an os/arch pair like linux/arm64")
        return platforms

    @field_validator("services", mode="before")
    @classmethod
    def _coerce_services(cls, services: object) -> object:
        # String-form ports name their owner by context (the entry or option they sit on);
        # group-level ports must spell the mapping form because a group has no namesake service.
        # Shapes that are not mappings ride through untouched; field validation reports them.
        if not is_mapping(services):
            return services
        for name, raw_entry in services.items():
            if not is_mapping(raw_entry):
                continue
            if "options" in raw_entry:
                raw_options: object = raw_entry["options"] or {}
                if not is_mapping(raw_options):
                    continue
                if not raw_options:
                    raise ValueError(f"services.{name}: a group's options: is a non-empty map of alternatives")
                for option_name, raw_option in raw_options.items():
                    if not is_mapping(raw_option):
                        continue
                    _coerce_port_strings(
                        f"services.{name}.options.{option_name}", raw_option, option_name, bare_legal=True
                    )
                _coerce_port_strings(f"services.{name}", raw_entry, name, bare_legal=False)
            else:
                _coerce_port_strings(f"services.{name}", raw_entry, name, bare_legal=True)
        return services

    @field_validator("mirror_prefix")
    @classmethod
    def _require_bare_prefix(cls, mirror_prefix: str) -> str:
        if not mirror_prefix or mirror_prefix.endswith("/"):
            raise ValueError("mirror prefix must be non-empty and carry no trailing slash")
        return mirror_prefix

    @model_validator(mode="after")
    def _require_flat_vocabulary(self) -> Self:
        # Entry and option names share one flat vocabulary.
        entry_names = set(self.services)
        claimed: set[str] = set()
        for name, entry in self.services.items():
            if isinstance(entry, ServiceGroup):
                for option_name in entry.options:
                    if option_name in entry_names or option_name in claimed:
                        raise ValueError(
                            f"services.{name}.options.{option_name}: entry and option names share one vocabulary; "
                            f"{option_name} is already taken"
                        )
                    claimed.add(option_name)
        return self

    @model_validator(mode="after")
    def _require_unique_route_paths(self) -> Self:
        # Route paths are edge-wide unique.
        owner: dict[str, str] = {}
        for where, route in self._route_sites():
            if route.path in owner:
                raise ValueError(
                    f"{where}: route path {route.path} is already declared at {owner[route.path]}; "
                    "paths are edge-wide unique"
                )
            owner[route.path] = where
        return self

    @model_validator(mode="after")
    def _require_auth_shape(self) -> Self:
        # Auth is a reserved group name; an auth option declares exactly one route — its
        # upstream is the auth entry point the edge forwards headers for.
        if AUTH_GROUP_NAME in self.services:
            auth = self.services[AUTH_GROUP_NAME]
            if not isinstance(auth, ServiceGroup):
                raise ValueError(f"services.{AUTH_GROUP_NAME}: auth is a reserved group name — shape it via options:")
            for option_name, option in auth.options.items():
                if len(option.routes) != 1:
                    raise ValueError(
                        f"services.{AUTH_GROUP_NAME}.options.{option_name}: an auth option declares exactly one "
                        "route; its upstream is the auth entry point the edge forwards headers for"
                    )
        return self

    @model_validator(mode="after")
    def _require_gateway_pairing(self) -> Self:
        # The edge emits iff any route rides anywhere.
        any_route = any(self._route_sites())
        if any_route and self.gateway is None:
            raise ValueError("routes are declared; gateway (the edge's host port) is required")
        if not any_route and self.gateway is not None:
            raise ValueError("gateway is declared but no route exists anywhere; the edge would never emit")
        return self

    @model_validator(mode="after")
    def _require_project_pairings(self) -> Self:
        if self.services:
            if not self.project_name:
                raise ValueError("services are declared; project_name is required (the compose project name)")
            if self._any_local_entries() and not self.repository:
                raise ValueError(
                    "local entries (workloads:) are declared; repository is required — published artifacts "
                    "derive their names from it"
                )
        return self

    @model_validator(mode="after")
    def _require_platforms_with_base_images(self) -> Self:
        # The rule guards lock resolution: one platform manifest per declared os/arch.
        # Charts are arch-neutral and exempt; built services alone never force it.
        if self.base_images and not self.platforms:
            raise ValueError(
                "base_images are declared; platforms is required beside them — an amd64-only repo declares "
                "[linux/amd64]"
            )
        return self

    def finalize(self) -> None:
        # Filesystem-gated laws: the leftover-manifest guard, the service derivation, and
        # the pairing laws that need the derived service set.
        assert self.root is not None
        root = self.root
        if (root / LEFTOVER_MANIFEST_PATH).exists():
            raise RuntimeError(
                f"{LEFTOVER_MANIFEST_PATH.as_posix()} exists; the manifest dissolved — its declarations live in "
                f"{DEFAULT_CONFIG_PATH.as_posix()}'s root base_images/repository keys. Delete the leftover."
            )
        if (root / LEFTOVER_LOCK_PATH).exists():
            raise RuntimeError(
                f"{LEFTOVER_LOCK_PATH.as_posix()} exists; the lock lives at {LOCK_PATH.as_posix()} in the repo root. "
                "Move the file (git mv)."
            )

        self.base_images = _resolve_base_image_templates(root, self.base_images)

        service_builds = derive_services(root, self.variants)
        if service_builds and self.repository is None:
            listed = ", ".join(sorted(service_builds))
            raise RuntimeError(
                f"Dockerfile service directories exist ({listed}); repository is required — it derives every image tag"
            )
        self.service_builds = service_builds

        declared_charts = self.declared_charts
        if (self.base_images or declared_charts) and not (root / LOCK_PATH).is_file():
            raise RuntimeError(
                f"{LOCK_PATH.as_posix()}: base_images or k8s.charts are declared, so the committed image lock is "
                "required beside them (the lock verb writes it)"
            )
        if self.gateway is not None:
            # A cutover (env-line) lock skips the CADDY check here: its parse refusal fires
            # in every consumer instead, and the lock verb must stay runnable to rewrite it.
            entries = parse_committed_lock(root)
            if entries is not None:
                entries.require(
                    CADDY_IMAGE,
                    "routes are declared, so the edge rides",
                    hint="declare CADDY_IMAGE in base_images and run lock",
                )
        for chart in declared_charts:
            values = self.chart_values_path(chart)
            if not values.is_file():
                raise RuntimeError(f"{values.as_posix()}: the chart's values file is required at the derived path")


def _coerce_port_strings(where: str, entry: dict[str, object], owning_name: str, *, bare_legal: bool) -> None:
    # A non-list ports value rides through untouched; field validation reports it.
    ports: object = entry.get("ports") or []
    if not is_list(ports):
        return
    coerced: list[object] = []
    for item in ports:
        if is_mapping(item):
            coerced.append(item)
            continue
        if not isinstance(item, str):
            raise TypeError(f"{where}.ports: each entry is a published/target[/protocol] string or a mapping")
        if not bare_legal:
            raise ValueError(
                f"{where}.ports: a group has no namesake service — spell the mapping form (service, published, target)"
            )
        parts = item.split("/")
        if len(parts) == 2:
            published, target, protocol = parts[0], parts[1], "tcp"
        elif len(parts) == 3:
            published, target, protocol = parts
        else:
            raise ValueError(f"{where}.ports entry {item!r} is not a published/target[/protocol] string")
        coerced.append({"service": owning_name, "published": published, "target": target, "protocol": protocol})
    entry["ports"] = coerced


def is_mapping(value: object) -> TypeIs[dict[str, object]]:
    # The shared yaml-shape predicates: markers, secrets, and the config pre-passes narrow
    # through these.
    return isinstance(value, dict)


def is_list(value: object) -> TypeIs[list[object]]:
    return isinstance(value, list)


def _resolve_base_image_templates(root: Path, base_images: dict[str, str]) -> dict[str, str]:
    # Toolchain components in base_images derive by construction: {uv} from
    # tool.uv.required-version, {python} from .python-version. Plain values stay plain —
    # a literal in place of a template is the deliberate-skew escape hatch, visible in
    # every diff.
    pins: dict[str, str] = {}
    used = sorted({key for reference in base_images.values() for key in TEMPLATE_KEY_PATTERN.findall(reference)})
    for key in used:
        if key == "uv":
            pins[key] = _uv_version_pin(root)
        elif key == "python":
            pins[key] = _python_version_pin(root)
        else:
            raise RuntimeError(
                f"base_images template key {{{key}}} is unknown; the toolchain pins are {{uv}}, {{python}}"
            )
    resolved: dict[str, str] = {}
    for name, reference in base_images.items():
        parts: list[str] = []
        cursor = 0
        for match in TEMPLATE_KEY_PATTERN.finditer(reference):
            parts.append(reference[cursor : match.start()])
            parts.append(pins[match.group(1)])
            cursor = match.end()
        parts.append(reference[cursor:])
        resolved[name] = "".join(parts)
    return resolved


def _uv_version_pin(root: Path) -> str:
    path = root / PYPROJECT_PATH
    document: dict[str, object] = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    tool_table: object = document.get("tool")
    uv_table: object = tool_table.get("uv") if is_mapping(tool_table) else None
    declared: object = uv_table.get("required-version") if is_mapping(uv_table) else None
    if not isinstance(declared, str) or not declared:
        raise RuntimeError(
            f"{PYPROJECT_PATH.as_posix()}: tool.uv.required-version is not declared; "
            "a {uv} template resolves from that exact pin"
        )
    match = UV_EXACT_PIN_PATTERN.fullmatch(declared.strip())
    if match is None:
        raise RuntimeError(
            f"{PYPROJECT_PATH.as_posix()}: tool.uv.required-version {declared!r} is a range, not an exact pin; "
            "a {uv} template resolves only from an exact == pin like '==0.12.15'"
        )
    return match.group("version")


def _python_version_pin(root: Path) -> str:
    path = root / PYTHON_VERSION_PATH
    if not path.is_file():
        raise RuntimeError(
            f"{PYTHON_VERSION_PATH.as_posix()}: the file is absent; "
            "a {python} template resolves from its declared version"
        )
    # uv reads the first line as the default selection when several are listed.
    version = next((line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()), "")
    if not version:
        raise RuntimeError(
            f"{PYTHON_VERSION_PATH.as_posix()}: the file is empty; "
            "a {python} template resolves from its declared version"
        )
    return version


class ImageLockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: str
    platforms: dict[str, str] = Field(default_factory=dict)

    @field_validator("index", "platforms")
    @classmethod
    def _require_digest_pins(cls, value: str | dict[str, str], info: ValidationInfo) -> str | dict[str, str]:
        context: dict[str, object] = info.context or {}
        source = context.get("path", "the image lock")
        pins = value.items() if isinstance(value, dict) else [(info.field_name, value)]
        for site, reference in pins:
            if not reference.rpartition("@")[2].startswith("sha256:"):
                raise ValueError(f"{source}: entry field {site} is not digest-pinned ({reference!r})")
        if isinstance(value, dict):
            for platform in value:
                if PLATFORM_PATTERN.fullmatch(platform) is None:
                    raise ValueError(f"{source}: platform key {platform!r} must be an os/arch pair like linux/arm64")
        return value

    @property
    def recorded_digests(self) -> list[str]:
        # The digest set the up/mirror presence probes walk: the index plus every platform manifest.
        return [self.index, *self.platforms.values()]


class ImageLock(BaseModel):
    # The structured lock beside the root config: registry-resolved facts, written by the
    # lock verb and build's incremental freeze, never hand-authored.
    model_config = ConfigDict(extra="forbid")

    images: dict[str, ImageLockEntry] = Field(default_factory=dict)
    charts: dict[str, str] = Field(default_factory=dict)

    @field_validator("charts")
    @classmethod
    def _require_digest_pinned_charts(cls, charts: dict[str, str], info: ValidationInfo) -> dict[str, str]:
        context: dict[str, object] = info.context or {}
        for name, reference in charts.items():
            if not reference.rpartition("@")[2].startswith("sha256:"):
                raise ValueError(
                    f"{context.get('path', 'the image lock')}: chart {name} is not digest-pinned ({reference!r})"
                )
        return charts

    @model_validator(mode="after")
    def _require_one_flat_vocabulary(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        shared = sorted(self.images.keys() & self.charts.keys())
        if shared:
            raise ValueError(
                f"{context.get('path', 'the image lock')}: names {shared} appear under both images and charts; "
                "the env projection carries one flat vocabulary"
            )
        return self

    def require(self, name: str, subject: str, hint: str = "the lock verb writes it") -> str:
        # The one "lock lacks NAME" refusal every lock consumer raises; the image entry
        # answers by its index reference, the chart entry flat.
        if name in self.images:
            return self.images[name].index
        if name in self.charts:
            return self.charts[name]
        raise RuntimeError(f"{subject}: {LOCK_PATH.as_posix()} lacks the {name} entry ({hint})")


class ComposeBundle(NamedTuple):
    name: str
    output: Path
    workloads: list[str]
    vars: dict[str, str]


class ToolRequirement(NamedTuple):
    binary: str
    version: str
    release: str
    archive: str
    extract: str


def _reject_duplicate_keys(node: yaml.Node, source: str) -> None:
    # The stock loader silently keeps the last of duplicate mapping keys; the lock
    # refuses them, with the offending line from the node mark.
    if isinstance(node, yaml.MappingNode):
        seen: set[object] = set()
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, yaml.ScalarNode) else repr(key_node.value)
            if key in seen:
                raise ValueError(f"{source}: line {key_node.start_mark.line + 1}: duplicate key {key!r}")
            seen.add(key)
            _reject_duplicate_keys(value_node, source)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _reject_duplicate_keys(item, source)


def parse_committed_lock(root: Path) -> ImageLock | None:
    # The lenient cutover-lock read: an absent lock answers the empty lock, an unparseable
    # one (the env-line format before the structured flip) answers None rather than
    # refusing — the rewrite is the cutover migration mechanism, so the lock verb and the
    # CADDY check stay runnable over a cutover lock.
    path = root / LOCK_PATH
    if not path.is_file():
        return ImageLock()
    try:
        return parse_image_lock(path)
    except (TypeError, ValueError):
        return None


def parse_image_lock(path: Path) -> ImageLock:
    text = path.read_text(encoding="utf-8")
    source = path.as_posix()
    try:
        document_node = yaml.SafeLoader(text).get_single_node()
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f"{source}:{mark.line + 1}" if mark is not None else source
        raise ValueError(f"{location}: {error}") from error
    if document_node is not None:
        _reject_duplicate_keys(document_node, source)
    loaded: object = yaml.safe_load(text)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise TypeError(
            f"{source}: the lock is a YAML mapping of images:/charts: sections, "
            f"not a bare {type(loaded).__name__} (the env-line format died with the structured flip; "
            "the lock verb rewrites cutover locks)"
        )
    return ImageLock.model_validate(loaded, context={"path": source})


def mirrored_ref(reference: str, prefix: str) -> str:
    # The only prefix join: it rejects an already-prefixed reference (the double-prefix
    # guard — drift toward the mirror namespace is a loud error, never a silent skip).
    if reference.startswith(f"{prefix}/"):
        raise RuntimeError(f"declarations are upstream-native; {reference} already carries the mirror prefix {prefix}")
    return f"{prefix}/{reference}"


def _find(parent: dict[str, str], service: str) -> str:
    root = service
    while parent[root] != root:
        root = parent[root]
    while parent[service] != service:
        parent[service], service = root, parent[service]
    return root


def load_config(root: Path) -> DockerDevkitConfig | None:
    path = root / DEFAULT_CONFIG_PATH
    if not path.exists():
        return None
    config = DockerDevkitConfig.model_validate(load_strict_yaml(path.read_text(encoding="utf-8")).data)
    installed = importlib.metadata.version("docker-devkit")
    if installed != DEV_SENTINEL and not SpecifierSet(config.requires).contains(installed, prereleases=True):
        raise SystemExit(f"docker-devkit {installed} does not satisfy requires={config.requires!r}")
    config.root = root
    config.finalize()
    return config


def service_sha_env(name: str) -> str:
    return name.upper().replace("-", "_") + "_SHA"


def derive_services(root: Path, axes: dict[str, list[str]] | None = None) -> dict[str, ServiceBuild]:
    # Every Dockerfile-bearing directory one level below a top-level directory is a
    # service; the graph (context edges, variant axes, SHA args) derives from the
    # Dockerfiles themselves. Parsing and variant resolution complete before any
    # ServiceBuild is constructed — each record is built once, fully resolved.
    declared_axes = axes if axes is not None else {}
    parsed = _parse_dockerfiles(root, declared_axes)

    computable = {service_sha_env(name) for name in parsed}
    for service in parsed.values():
        unknown = [arg for arg in service.sha_args if arg not in computable]
        if unknown:
            raise RuntimeError(
                f"workloads/{service.name}/{DOCKERFILE_NAME}: ARG {', '.join(unknown)} names no derived service; "
                "the stamp receiver must match a service directory"
            )

    resolved: dict[str, tuple[str | None, list[str]]] = {}
    builds: dict[str, ServiceBuild] = {}
    for name, service in parsed.items():
        axis, variants = _resolve_axis(name, (), parsed, resolved, declared_axes)
        builds[name] = ServiceBuild(
            name=name,
            dockerfile=service.dockerfile,
            contexts=service.contexts,
            axis=axis,
            variants=variants,
            sha_args=service.sha_args,
            dev_stage=service.dev_stage,
        )
    return builds


def _parse_dockerfiles(root: Path, axes: dict[str, list[str]]) -> dict[str, ParsedDockerfile]:
    service_dirs: dict[str, Path] = {}
    tops: set[str] = set()
    for top in sorted(root.iterdir()):
        if not top.is_dir() or top.name.startswith("."):
            continue
        for child in sorted(top.iterdir()):
            if child.is_dir() and (child / DOCKERFILE_NAME).is_file():
                service_dirs[child.name] = child
                tops.add(top.name)
    _require_single_top(tops, "Dockerfile service directories")

    parsed: dict[str, ParsedDockerfile] = {}
    for name, directory in service_dirs.items():
        dockerfile = directory / DOCKERFILE_NAME
        relative = dockerfile.relative_to(root).as_posix()
        from_images: list[str] = []
        copy_from_values: list[str] = []
        stages: set[str] = set()
        args: list[str] = []
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            from_match = FROM_LINE_PATTERN.match(line.strip())
            if from_match is not None:
                from_images.append(from_match.group(1))
                if from_match.group(2) is not None:
                    stages.add(from_match.group(2))
                continue
            copy_from_match = COPY_FROM_LINE_PATTERN.match(line.strip())
            if copy_from_match is not None:
                copy_from_values.append(copy_from_match.group(1))
                continue
            arg_match = ARG_LINE_PATTERN.match(line.strip())
            if arg_match is not None:
                args.append(arg_match.group(1))
        contexts: list[str] = []
        for image in from_images:
            if image in stages:
                continue
            if BARE_IDENTIFIER_PATTERN.fullmatch(image) is None:
                if BUILD_ARG_REFERENCE_PATTERN.fullmatch(image) is None:
                    raise RuntimeError(
                        f"{relative}: FROM {image} is a hand-written image reference; "
                        "declare the base under base_images and FROM its build arg"
                    )
                continue
            if image not in service_dirs:
                raise RuntimeError(
                    f"{relative}: FROM {image} names no service directory; "
                    "a bare identifier in FROM must name a sibling service or a stage"
                )
            contexts.append(image)
        for value in copy_from_values:
            if "/" in value:
                raise RuntimeError(
                    f"{relative}: COPY --from={value} is a hand-written image reference; "
                    "declare the base under base_images and COPY from its build arg"
                )
        declared_axis_args = [argument for argument in args if argument in axes]
        if len(declared_axis_args) > 1:
            raise RuntimeError(
                f"{relative}: declares two variant-axis ARGs ({', '.join(declared_axis_args)}); "
                "a service carries one axis"
            )
        parsed[name] = ParsedDockerfile(
            name=name,
            dockerfile=dockerfile.relative_to(root).as_posix(),
            contexts=sorted(set(contexts)),
            sha_args=sorted({arg for arg in args if arg.endswith("_SHA")}),
            # A genuine dev stage is one of several stages; a single-stage AS dev label
            # names the published image and is not a dev shape.
            dev_stage="dev" in stages and len(stages) > 1,
            axis_arg=declared_axis_args[0] if declared_axis_args else None,
        )
    return parsed


def _require_single_top(tops: set[str], subject: str) -> None:
    if len(tops) > 1:
        raise RuntimeError(f"{subject} span multiple top-level directories: {sorted(tops)}")


def _resolve_axis(
    name: str,
    seen: tuple[str, ...],
    parsed: dict[str, ParsedDockerfile],
    resolved: dict[str, tuple[str | None, list[str]]],
    axes: dict[str, list[str]],
) -> tuple[str | None, list[str]]:
    if name in resolved:
        return resolved[name]
    if name in seen:
        raise RuntimeError(f"FROM context cycle: {' -> '.join((*seen, name))}")
    parent_results = [_resolve_axis(parent, (*seen, name), parsed, resolved, axes) for parent in parsed[name].contexts]
    distinct = {(axis, tuple(variants)) for axis, variants in parent_results}
    if len(distinct) > 1:
        parents = ", ".join(parsed[name].contexts)
        raise RuntimeError(
            f"workloads/{name}/{DOCKERFILE_NAME}: parents ({parents}) disagree on variant cells; "
            "a dependent inherits its base's variant cells"
        )
    if parent_results:
        if parsed[name].axis_arg is not None:
            raise RuntimeError(
                f"workloads/{name}/{DOCKERFILE_NAME}: declares ARG {parsed[name].axis_arg} over a "
                "variant-bearing base; a dependent inherits its base's variant cells"
            )
        axis, variants = parent_results[0]
        resolved[name] = (axis, list(variants))
    else:
        axis = parsed[name].axis_arg
        resolved[name] = (axis, list(axes[axis])) if axis is not None else (None, [])
    return resolved[name]


def compute_service_shas(repo_root: Path, service_builds: dict[str, ServiceBuild]) -> dict[str, str]:
    repo_root = repo_root.resolve()

    service_dirs = {str(PurePosixPath(service.dockerfile).parent) for service in service_builds.values()}

    if not service_dirs:
        # A repo with no Dockerfile services (mirror-only, or bundles built elsewhere)
        # has nothing to hash; skip the git and .dockerignore reads entirely.
        return {}

    # Derive the workload root as the single top-level directory holding every service;
    # files under it that belong to no service are excluded from every SHA.
    tops = {PurePosixPath(service_dir).parts[0] for service_dir in service_dirs}
    _require_single_top(tops, "services")
    workload_prefix = f"{next(iter(tops))}/"

    tree_entries = bash_output("git ls-tree -r HEAD", cwd=repo_root).splitlines()

    dockerignore = repo_root / ".dockerignore"
    spec = pathspec.PathSpec.from_lines("gitignore", dockerignore.read_text().splitlines())

    allowed_entries: list[tuple[str, str, str]] = []
    for entry in tree_entries:
        meta, path = entry.split("\t", 1)
        if spec.match_file(path):
            continue
        mode, _type, obj_hash = meta.split()
        allowed_entries.append((mode, obj_hash, path))

    shared_entries: list[tuple[str, str, str]] = []
    per_service: dict[str, list[tuple[str, str, str]]] = {directory: [] for directory in service_dirs}

    for mode, obj_hash, path in allowed_entries:
        if path.startswith(workload_prefix):
            for service_dir in service_dirs:
                if path.startswith(service_dir + "/"):
                    per_service[service_dir].append((mode, obj_hash, path))
                    break
        else:
            shared_entries.append((mode, obj_hash, path))

    result: dict[str, str] = {}
    for service_dir in sorted(service_dirs):
        entries = shared_entries + per_service[service_dir]
        index_input = "\n".join(f"{mode} {obj_hash}\t{path}" for mode, obj_hash, path in entries) + "\n"

        with TemporaryDirectory() as tmpdir:
            index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
            bash("git update-index --index-info", cwd=repo_root, env=index_env, stdin_text=index_input)
            tree_hash = bash_output("git write-tree", cwd=repo_root, env=index_env).strip()

        result[service_sha_env(PurePosixPath(service_dir).name)] = f"tree-{tree_hash}"

    return result
