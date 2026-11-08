from __future__ import annotations

import os
from collections.abc import Mapping, Set as AbstractSet
from pathlib import Path
from typing import Annotated, NamedTuple, Self

from pydantic import (
    BeforeValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from boatyard import dockerfiles
from boatyard.config.config import (
    EDGE_LISTEN_PORT,
    EDGE_SERVICE_NAME,
    ENV_NAME_PATTERN,
    OCI_REFERENCE_PREFIX,
    PLACEHOLDER,
    PortNumber,
    RESERVED_SLOT_MARKERS,
    ConfigFile,
    SlotDemand,
    Yaml,
    artifact_slot_demands,
    bundle_lock_key,
    compose_bundle_output_path,
    env_lock_slot,
    env_tree_slot,
    is_mapping,
    qualified_option_name,
    refuse_unresolved_references,
)
from boatyard.config.lock import LockFile
from boatyard.paths import DEFAULT_CONFIG_PATH, LOCK_PATH, VALUES_FILE
from boatyard.yaml import load_yaml_model

PUBLIC_URL_SLOT = "PUBLIC_URL"
EDGE_BASE_URL_SLOT = "EDGE_BASE_URL"

OVERRIDE_LAYOUT = ("stack", "generated", "compose")


def _coerce_section(values: object) -> dict[str, str]:
    if not is_mapping(values):
        raise ValueError("a section is a NAME: value mapping")
    coerced: dict[str, str] = {}
    for name, value in values.items():
        if isinstance(value, bool):
            coerced[name] = str(value).lower()
        elif isinstance(value, (int, float, str)):
            coerced[name] = str(value)
        else:
            raise TypeError(f"{name}: a section value is a scalar, not {type(value).__name__}")
    return coerced


SectionMap = Annotated[dict[str, str], BeforeValidator(_coerce_section)]


class PublicUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw: str
    forwarded_proto: str
    forwarded_port: str

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: str) -> dict[str, str]:
        scheme, separator, rest = value.partition("://")
        if not separator:
            raise ValueError(f"public_url: {value!r} must carry a scheme (https://example.dev)")
        hostport = rest.partition("/")[0]
        if ":" in hostport:
            forwarded_port = hostport.rpartition(":")[2]
        elif scheme == "https":
            forwarded_port = "443"
        else:
            forwarded_port = "80"
        return {"raw": value, "forwarded_proto": scheme, "forwarded_port": forwarded_port}

    @property
    def forwarded(self) -> dict[str, Yaml]:
        return {"FORWARDED_PROTO": self.forwarded_proto, "FORWARDED_PORT": self.forwarded_port}


class ValuesSections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: dict[str, SectionMap] = Field(default_factory=dict)
    bundles: dict[str, SectionMap] = Field(default_factory=dict)
    shared: SectionMap = Field(default_factory=dict)


class AssemblyPlan(NamedTuple):
    includes: list[str]
    values: dict[str, str]
    lock_entries: dict[str, str]
    tree_entries: dict[str, str]
    demanded: list[str]


class ValuesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundles: list[str] = Field(default_factory=list)
    path_overrides: dict[str, Path] = Field(default_factory=dict)
    public_url: PublicUrl | None = None
    platform: str | None = None
    gateway: PortNumber | None = None
    ports: dict[str, PortNumber] = Field(default_factory=dict)
    values: ValuesSections = Field(default_factory=ValuesSections)
    choices: dict[str, str] = Field(default_factory=dict, exclude=True)

    _config: ConfigFile | None = PrivateAttr(default=None)
    _lock: LockFile | None = PrivateAttr(default=None)
    _demands: dict[str, dict[str, SlotDemand]] = PrivateAttr(default_factory=dict)
    _participating: set[str] = PrivateAttr(default_factory=set)
    _homes: dict[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _validate_selection_and_sections(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        config = context.get("config")
        self._config = config if isinstance(config, ConfigFile) else None
        self._lock = lock if isinstance(lock := context.get("lock"), LockFile) else None
        if not isinstance(config, ConfigFile):
            if self.bundles:
                raise ValueError("bundles: is set but this repo declares no services")
            return self
        source = str(context.get("path", VALUES_FILE.name))

        selectable = config.selectable_options
        bare = config.bare_entries
        unknown = [name for name in self.bundles if name not in selectable]
        if unknown:
            message = f"bundles: names {unknown} are not among the selectable options {sorted(selectable)}"
            if bare:
                message += f" (bare entries, always included: {bare})"
            raise ValueError(message)
        choices: dict[str, str] = {}
        for name in self.bundles:
            group, option_name = selectable[name]
            if group in choices:
                raise ValueError(
                    f"bundles: {name} and {qualified_option_name(group, choices[group])} "
                    f"are two options of one group ({group})"
                )
            choices[group] = option_name
        self.choices = choices
        self._participating = {service.name for service in config.resolve_services(choices)}

        vocabulary = sorted(config.port_declaring_entries)
        unknown = sorted(set(self.ports) - set(vocabulary))
        if unknown:
            raise ValueError(f"{source}: ports: names {unknown} are not among the port-declaring entries {vocabulary}")
        if self.gateway is not None and config.gateway is None:
            raise ValueError(f"{source}: gateway: is set but boatyard.yml declares no gateway")

        demands: dict[str, dict[str, SlotDemand]] = dict(config.consumer_surfaces)
        homes = dict.fromkeys(config.consumer_surfaces, "images")
        remote_names = {name for name, _reference, _entry in config.remote_declared_entries()}
        for name in remote_names:
            homes[name] = "bundles"
            if self._lock is not None:
                record = self._lock.bundles.get(bundle_lock_key(name))
                if record is not None:
                    demands[name] = dict(record.slots)
        for name in sorted(self._participating):
            if name not in self.path_overrides or name not in remote_names:
                continue
            demands[name] = artifact_slot_demands(
                [self._override_bundle_path(name, config.root).read_text(encoding="utf-8")]
            )
        self._demands = demands
        self._homes = homes

        local_names = sorted(name for name, home in self._homes.items() if home == "images")
        sorted_remote_names = sorted(name for name, home in self._homes.items() if home == "bundles")
        demanders: dict[str, set[str]] = {}
        for unit, slots in self._demands.items():
            for name in slots:
                demanders.setdefault(name, set()).add(unit)

        for section_name, expected_home, noun, other_names in (
            ("images", "images", "local", local_names),
            ("bundles", "bundles", "ref", sorted_remote_names),
        ):
            entries = self.values.images if section_name == "images" else self.values.bundles
            for name in sorted(entries):
                if self._homes.get(name) != expected_home:
                    raise ValueError(
                        f"{source}: values.{section_name}.{name} is not a {noun} entry "
                        f"(the {noun} entries are {other_names})"
                    )
                site = f"values.{section_name}.{name}"
                surface = sorted(self._demands.get(name, {}))
                for key in sorted(entries[name]):
                    self._refuse_key_shape(key, site)
                    if key not in self._demands.get(name, {}):
                        raise ValueError(f"{site}: {key} is demanded by no surface of {name} (the surface is {surface})")
                    if demanders[key] - {name}:
                        raise ValueError(
                            f"{site}: {key} is demanded by {', '.join(sorted(demanders[key]))}, not {name} alone"
                        )

        for name in sorted(self.values.shared):
            self._refuse_key_shape(name, "values.shared")
            if name not in demanders:
                raise ValueError(f"{source}: values.shared: {name} is demanded by no surface")
            if len(demanders[name]) == 1:
                [home] = demanders[name]
                raise ValueError(f"{source}: values.shared: {name} is demanded by {home} only")

        sections: dict[str, Mapping[str, str]] = {"values.shared": self.values.shared}
        sections.update((f"values.images.{unit}", section) for unit, section in self.values.images.items())
        sections.update((f"values.bundles.{unit}", section) for unit, section in self.values.bundles.items())
        references: dict[str, set[str]] = {}
        sites: dict[str, str] = {}
        for site, section in sorted(sections.items()):
            for name, value in section.items():
                if "${" in PLACEHOLDER.sub("", value):
                    raise ValueError(
                        f"{source}: {site}: {name}: only bare ${{NAME}} references are legal — "
                        f"no defaults, operators, filters, or nesting"
                    )
                for reference in PLACEHOLDER.findall(value):
                    if any(marker in reference for marker in RESERVED_SLOT_MARKERS):
                        raise ValueError(f"{source}: {site}: {name}: ${{{reference}}} must not carry a reserved marker")
                    references.setdefault(name, set()).add(reference)
                    sites[name] = site
        roots = set(self.flattened) | set(self.minted)
        unresolvable = sorted({reference for referenced in references.values() for reference in referenced} - roots)
        if unresolvable:
            name = min(name for name, referenced in references.items() if referenced & set(unresolvable))
            raise ValueError(
                f"{source}: {sites[name]}: {name}: ${{{unresolvable[0]}}} resolves to no root "
                f"(the roots are {sorted(roots)})"
            )
        hand_edges = {
            name: {reference for reference in PLACEHOLDER.findall(value) if reference in self.flattened}
            for name, value in self.flattened.items()
        }
        _order, blocked = peel_references(hand_edges)
        if blocked:
            cycle_nodes = {name: hand_edges[name] & set(blocked) for name in blocked}
            # Every blocked name keeps a blocked successor, so the walk repeats a node exactly on the cycle
            seen: dict[str, int] = {}
            chain: list[str] = []
            node = min(cycle_nodes)
            while node not in seen:
                seen[node] = len(chain)
                chain.append(node)
                node = min(cycle_nodes[node])
            cycle = chain[seen[node] :]
            raise ValueError(f"{source}: reference cycle: {' -> '.join([*cycle, cycle[0]])}")

        filled = set(self.values.shared)
        for name, home_section in self._homes.items():
            if home_section == "images" and name in self.values.images:
                filled.update(self.values.images[name])
            elif name in self.values.bundles:
                filled.update(self.values.bundles[name])
        if self.public_url is not None:
            filled.add(PUBLIC_URL_SLOT)
        if self._config is not None and self._config.gateway is not None:
            filled.add(EDGE_BASE_URL_SLOT)
        missing: dict[str, list[str]] = {}
        for unit in sorted(self._participating):
            unfilled = sorted(
                name for name, demand in self._demands.get(unit, {}).items() if demand.required and name not in filled
            )
            if unfilled:
                missing[unit] = unfilled
        if missing:
            unit, absent = min(missing.items())
            ignored = sorted(shell_name for shell_name in absent if os.environ.get(shell_name))
            note = f" ({', '.join(ignored)} are set in the shell and ignored)" if ignored else ""
            raise ValueError(
                f"{source}: values.{self._homes[unit]}.{unit} requires {absent}, which no declared source fills{note}"
            )

        declared: set[str] = set(config.secrets.root) if config.secrets is not None else set()
        covered = set(self.values.shared)
        for values_section in (*self.values.images.values(), *self.values.bundles.values()):
            covered.update(values_section)
        undeclared = sorted({name for slots in self._demands.values() for name in slots} - declared - covered)
        if undeclared:
            raise ValueError(f"{source}: consumed keys {undeclared} are declared nowhere (boatyard.yml secrets or values keys)")

        bundle_vocabulary = set(config.selectable_options) | set(config.bare_entries)
        unknown_overrides: list[str] = []
        local_override_names: list[str] = []
        for name in self.path_overrides:
            if name not in bundle_vocabulary:
                unknown_overrides.append(name)
            elif name not in remote_names:
                local_override_names.append(name)
        if unknown_overrides:
            raise ValueError(
                f"path_overrides: names {sorted(unknown_overrides)} "
                f"are not in the bundle vocabulary {sorted(bundle_vocabulary)}"
            )
        if local_override_names:
            raise ValueError(
                f"path_overrides: names {sorted(local_override_names)} are not ref entries "
                f"(the ref entries are {sorted(remote_names)})"
            )
        return self

    @field_validator("path_overrides", mode="before")
    @classmethod
    def _require_path_values(cls, raw_overrides: object) -> object:
        if not is_mapping(raw_overrides):
            return raw_overrides
        path_overrides: dict[str, Path] = {}
        for name, value in raw_overrides.items():
            stripped = str(value).strip()
            if not stripped:
                raise ValueError(f"path_overrides.{name} must be a non-empty filesystem path")
            if "://" in stripped:
                raise ValueError(f"path_overrides.{name} must be a filesystem path, not a URL ({stripped!r})")
            path_overrides[name] = Path(stripped)
        return path_overrides

    @field_validator("path_overrides")
    @classmethod
    def _require_output_layout(cls, path_overrides: dict[str, Path]) -> dict[str, Path]:
        for name, path in path_overrides.items():
            if path.parts[-4:-1] != OVERRIDE_LAYOUT:
                raise ValueError(
                    f"path_overrides.{name}: {path.as_posix()!r} must point into a stack/generated/compose/ bundle output layout"
                )
        return path_overrides

    @classmethod
    def load(cls, path: Path, config: ConfigFile | None, lock: LockFile | None = None) -> ValuesFile:
        return load_yaml_model(cls, path, {"path": path.as_posix(), "config": config, "lock": lock})

    @property
    def flattened(self) -> dict[str, str]:
        values = dict(self.values.shared)
        for section in (*self.values.images.values(), *self.values.bundles.values()):
            values.update(section)
        return values

    @property
    def minted(self) -> dict[str, str]:
        minted: dict[str, str] = {}
        if self.public_url is not None:
            minted[PUBLIC_URL_SLOT] = self.public_url.raw
        if self._config is not None and self._config.gateway is not None:
            minted[EDGE_BASE_URL_SLOT] = f"http://{EDGE_SERVICE_NAME}:{EDGE_LISTEN_PORT}"
        return minted

    @property
    def resolved(self) -> dict[str, str]:
        hand = self.flattened
        references = {name: set(PLACEHOLDER.findall(value)) & hand.keys() for name, value in hand.items()}
        order, _blocked = peel_references(references)
        values = dict(self.minted)
        for name in order:
            parts = PLACEHOLDER.split(hand[name])
            values[name] = "".join(part if index % 2 == 0 else values[part] for index, part in enumerate(parts))
        return values

    def assembly(self) -> AssemblyPlan:
        # The heavy derivation only up pays for: the producer projection under the consumer
        # platform and the per-entry lock/tree fans over the resolved selection.
        config, lock = self._config, self._lock
        if config is None or lock is None:
            raise ValueError("assembly() requires ValuesFile.load(path, config, lock)")
        root = config.root
        lock_entries: dict[str, str] = {}
        tree_entries: dict[str, str] = {}
        shas_by_root: dict[Path, dict[str, str]] = {}
        includes: list[str] = []

        for service in config.resolve_services(self.choices):
            name = service.name

            # Include a remote ref as its lock-pinned digest
            if service.entry.ref is not None and name not in self.path_overrides:
                pinned = lock.require(bundle_lock_key(name), f"bundle {name!r}")
                includes.append(f"{OCI_REFERENCE_PREFIX}{pinned}")
                continue

            if service.entry.ref is None:
                bundle_compose_path = compose_bundle_output_path(root, name)
                if not bundle_compose_path.is_file():
                    raise ValueError(f"selected entry {name!r} lacks its bundle output {bundle_compose_path.as_posix()}")
                lock_references = lock.manifest_references(self.platform)
                tree_root, tree_builds = root, config.service_builds
            else:
                # Include an overridden ref through its local path
                bundle_compose_path = self._override_bundle_path(name, root).resolve()
                # The producer root sits four levels above a stack/generated/compose/ output
                producer_root = bundle_compose_path.parents[3]
                if not (producer_root / DEFAULT_CONFIG_PATH).is_file():
                    raise ValueError(
                        f"{bundle_compose_path.as_posix()}: no boatyard.yml-bearing repo root above the bundle override"
                    )
                # Project the producer lock's manifest references under the consumer's platform
                producer_config = ConfigFile.load(producer_root / DEFAULT_CONFIG_PATH)
                producer_lock = LockFile.load(producer_root / LOCK_PATH, producer_config)
                lock_references = producer_lock.manifest_references(self.platform)
                tree_root, tree_builds = producer_root, producer_config.service_builds

            # Refuse reserved services and unresolved route/port references against the bundle text
            text = bundle_compose_path.read_text(encoding="utf-8")
            service_names = refuse_unresolved_references(name, service.routes, service.ports, [text], prefix=f"{name}-")
            if EDGE_SERVICE_NAME in service_names:
                raise ValueError(
                    f"{bundle_compose_path.as_posix()}: "
                    f"the included bundle defines the reserved service {EDGE_SERVICE_NAME!r}"
                )

            # Include the file by absolute path and fan its lock keys under the marker
            includes.append(bundle_compose_path.resolve().as_posix())
            lock_entries.update({env_lock_slot(name, key): value for key, value in lock_references.items()})

            # Fan the source tree's content tags under the entry beside the lock fans
            if tree_root not in shas_by_root:
                shas_by_root[tree_root] = dockerfiles.compute_service_shas(tree_root, tree_builds)
            tree_entries.update({env_tree_slot(name, sha): value for sha, value in shas_by_root[tree_root].items()})

        demanded = sorted({name for unit in self._participating for name in self._demands.get(unit, {})})
        return AssemblyPlan(
            includes=includes,
            values=self.resolved,
            lock_entries=lock_entries,
            tree_entries=tree_entries,
            demanded=demanded,
        )

    def _refuse_key_shape(self, name: str, site: str) -> None:
        if ENV_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"key {name!r} is not an ALL_CAPS environment name")
        if any(marker in name for marker in RESERVED_SLOT_MARKERS):
            raise ValueError(f"key {name!r} must not carry a reserved __BOATYARD_LOCK__/__BOATYARD_TREE__ marker")
        if name == PUBLIC_URL_SLOT:
            raise ValueError(f"{site}: {name} is not hand-settable (minted from public_url)")
        if name == EDGE_BASE_URL_SLOT:
            raise ValueError(f"{site}: {name} is not hand-settable (minted from the gateway declaration)")

    def _override_bundle_path(self, name: str, root: Path) -> Path:
        candidate = self.path_overrides[name]
        path = candidate if candidate.is_absolute() else root / candidate
        if not path.is_file():
            raise ValueError(f"path_overrides.{name}: {path.as_posix()} does not exist")
        return path


def peel_references(references: Mapping[str, AbstractSet[str]]) -> tuple[list[str], list[str]]:
    # Answer the peel order (a name peels only after every name it references peels) and
    # the names a cycle leaves blocked
    blocked: dict[str, set[str]] = {name: set(targets) for name, targets in references.items()}
    order: list[str] = []
    ready = sorted(name for name, targets in blocked.items() if not targets)
    while ready:
        name = ready.pop(0)
        blocked.pop(name)
        order.append(name)
        newly_ready: list[str] = []
        for successor, targets in blocked.items():
            if not targets:
                continue
            targets.discard(name)
            if not targets:
                newly_ready.append(successor)
        ready.extend(sorted(newly_ready))
    return order, sorted(blocked)
