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
    EDGE_SERVICE_NAME,
    ENV_NAME_PATTERN,
    OCI_REFERENCE_PREFIX,
    PLACEHOLDER,
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
    scan_bundle_texts,
)
from boatyard.config.lock import LockFile
from boatyard.paths import DEFAULT_CONFIG_PATH, LOCK_PATH, VALUES_FILE
from boatyard.yaml import load_yaml_model

MINTED_SLOT = "PUBLIC_URL"

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
        if not isinstance(config, ConfigFile) or not config.services:
            if self.bundles:
                raise ValueError("bundles: is set but this repo declares no services")
            return self
        source = str(context.get("path", VALUES_FILE.name))

        self._fold_selection(config)
        self._build_demand_table(config, context)
        self._refuse_misplaced_keys(source)
        self._refuse_unresolvable_references(source)
        self._refuse_unfilled_slots(source)
        self._refuse_undeclared_demands(source, config)
        return self

    def _fold_selection(self, config: ConfigFile) -> None:
        selectable = config.selectable_options
        bare = config.bare_entries
        unknown = [name for name in self.bundles if name not in selectable]
        if unknown:
            bare_entries_note = f"; bare entries, always included: {bare}" if bare else ""
            raise ValueError(
                f"bundles: names {unknown}; the selectable options are {sorted(selectable)}{bare_entries_note}"
            )
        choices: dict[str, str] = {}
        for name in self.bundles:
            group, option_name = selectable[name]
            if group in choices:
                raise ValueError(
                    f"bundles: selects {qualified_option_name(group, choices[group])} and {name}; "
                    f"at most one option per group ({group})"
                )
            choices[group] = option_name
        self.choices = choices
        self._participating = {service.name for service in config.resolve_services(choices)}

    def _build_demand_table(self, config: ConfigFile, context: dict[str, object]) -> None:
        context_lock = context.get("lock")
        lock = context_lock if isinstance(context_lock, LockFile) else None
        demands: dict[str, dict[str, SlotDemand]] = dict(config.consumer_surfaces)
        homes = dict.fromkeys(config.consumer_surfaces, "images")
        remote_names = {name for name, _reference, _entry in config.remote_declared_entries()}
        for name in remote_names:
            homes[name] = "bundles"
            if lock is not None:
                record = lock.bundles.get(bundle_lock_key(name))
                if record is not None:
                    demands[name] = dict(record.slots)
        for name in sorted(self._participating):
            if name not in self.path_overrides or name not in remote_names:
                continue
            candidate = self.path_overrides[name]
            bundle_compose_path = candidate if candidate.is_absolute() else config.root / candidate
            if not bundle_compose_path.is_file():
                raise ValueError(f"path_overrides.{name}: {bundle_compose_path.as_posix()} does not exist")
            demands[name] = artifact_slot_demands([bundle_compose_path.read_text(encoding="utf-8")])
        self._demands = demands
        self._homes = homes

    def _refuse_misplaced_keys(self, source: str) -> None:
        local_names = sorted(name for name, home in self._homes.items() if home == "images")
        remote_names = sorted(name for name, home in self._homes.items() if home == "bundles")
        demanders: dict[str, set[str]] = {}
        for unit, slots in self._demands.items():
            for name in slots:
                demanders.setdefault(name, set()).add(unit)

        for name in sorted(self.values.images):
            if self._homes.get(name) != "images":
                raise ValueError(
                    f"{source}: values.images.{name}: the local entries are {local_names}; "
                    f"ref entries ride under values.bundles"
                )
            self._refuse_section_faults(f"values.images.{name}", self.values.images[name], name, demanders)
        for name in sorted(self.values.bundles):
            if self._homes.get(name) != "bundles":
                raise ValueError(
                    f"{source}: values.bundles.{name}: the ref entries are {remote_names}; "
                    f"local entries ride under values.images"
                )
            self._refuse_section_faults(f"values.bundles.{name}", self.values.bundles[name], name, demanders)
        for name in sorted(self.values.shared):
            self._refuse_key_shape(name, "values.shared")
            if name not in demanders:
                raise ValueError(f"{source}: values.shared: {name} is demanded by no surface")
            if len(demanders[name]) == 1:
                [home] = demanders[name]
                raise ValueError(
                    f"{source}: values.shared: {name} is demanded by {home} only; set it in "
                    f"values.{self._homes[home]}.{home}"
                )

    def _refuse_section_faults(
        self, site: str, section: dict[str, str], unit: str, demanders: dict[str, set[str]]
    ) -> None:
        surface = sorted(self._demands.get(unit, {}))
        for name in sorted(section):
            self._refuse_key_shape(name, site)
            if name not in self._demands.get(unit, {}):
                raise ValueError(f"{site}: {name} is demanded by no surface of {unit} (the surface is {surface})")
            if demanders[name] - {unit}:
                raise ValueError(
                    f"{site}: {name} is demanded by {', '.join(sorted(demanders[name]))}; set it in values.shared"
                )

    def _refuse_key_shape(self, name: str, site: str) -> None:
        if ENV_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"key {name!r} is not an ALL_CAPS environment name")
        if any(marker in name for marker in RESERVED_SLOT_MARKERS):
            raise ValueError(
                f"key {name!r} carries a reserved __BOATYARD_LOCK__/__BOATYARD_TREE__ marker; "
                f"the lock and the tree tags fill those slots — never set them by hand"
            )
        if name == MINTED_SLOT:
            raise ValueError(f"{site}: {name} is minted from public_url; never set it by hand")

    def _refuse_unresolvable_references(self, source: str) -> None:
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
                        raise ValueError(
                            f"{source}: {site}: {name}: ${{{reference}}} carries a reserved "
                            f"__BOATYARD_LOCK__/__BOATYARD_TREE__ marker; the lock and the tree "
                            f"tags fill those slots — never reference them"
                        )
                    references.setdefault(name, set()).add(reference)
                    sites[name] = site
        roots = set(self.flattened) | set(self.minted)
        unresolvable = sorted({reference for referenced in references.values() for reference in referenced} - roots)
        if unresolvable:
            name = min(name for name, referenced in references.items() if referenced & set(unresolvable))
            raise ValueError(
                f"{source}: {sites[name]}: {name}: ${{{unresolvable[0]}}} resolves to no root; "
                f"the roots are {sorted(roots)}"
            )
        hand_edges = {
            name: {reference for reference in PLACEHOLDER.findall(value) if reference in self.flattened}
            for name, value in self.flattened.items()
        }
        _order, blocked = peel_references(hand_edges)
        if blocked:
            cycle = reference_cycle({name: hand_edges[name] & set(blocked) for name in blocked})
            raise ValueError(f"{source}: reference cycle: {' -> '.join([*cycle, cycle[0]])}")

    def _refuse_unfilled_slots(self, source: str) -> None:
        filled = set(self.values.shared)
        for name, home in self._homes.items():
            if home == "images" and name in self.values.images:
                filled.update(self.values.images[name])
            elif name in self.values.bundles:
                filled.update(self.values.bundles[name])
        if self.public_url is not None:
            filled.add(MINTED_SLOT)
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
            note = f"; {', '.join(ignored)} set in the shell and ignored" if ignored else ""
            raise ValueError(
                f"{source}: values.{self._homes[unit]}.{unit} requires {absent}, "
                f"which no declared source fills (a values section or shared){note}"
            )

    def _refuse_undeclared_demands(self, source: str, config: ConfigFile) -> None:
        declared: set[str] = set(config.secrets.root) if config.secrets is not None else set()
        covered = set(self.values.shared)
        for section in (*self.values.images.values(), *self.values.bundles.values()):
            covered.update(section)
        undeclared = sorted({name for slots in self._demands.values() for name in slots} - declared - covered)
        if undeclared:
            raise ValueError(
                f"{source}: consumed keys {undeclared} are declared nowhere; declare them in "
                f"boatyard.yml secrets or cover them with values keys"
            )

    @field_validator("path_overrides", mode="before")
    @classmethod
    def _require_path_values(cls, raw_overrides: object) -> object:
        if not is_mapping(raw_overrides):
            return raw_overrides
        path_overrides: dict[str, Path] = {}
        for name, value in raw_overrides.items():
            stripped = str(value).strip()
            if not stripped:
                raise ValueError(f"path_overrides.{name}: empty; a value is a filesystem path")
            if "://" in stripped:
                raise ValueError(
                    f"path_overrides.{name}: {stripped!r} carries a URL scheme; a value is a filesystem path"
                )
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

    @model_validator(mode="after")
    def _require_ref_entry_overrides(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        config = context.get("config")
        if not isinstance(config, ConfigFile) or not config.services:
            return self
        vocabulary = set(config.selectable_options) | set(config.bare_entries)
        remote_names = {name for name, _declared, _entry in config.remote_declared_entries()}
        unknown_overrides: list[str] = []
        local_names: list[str] = []
        for name in self.path_overrides:
            if name not in vocabulary:
                unknown_overrides.append(name)
            elif name not in remote_names:
                local_names.append(name)
        if unknown_overrides:
            raise ValueError(
                f"path_overrides: names {sorted(unknown_overrides)}; the bundle vocabulary is {sorted(vocabulary)}"
            )
        if local_names:
            raise ValueError(
                f"path_overrides: names {sorted(local_names)}; an override targets a ref entry; "
                f"the ref entries are {sorted(remote_names)}"
            )
        return self

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
        return {MINTED_SLOT: self.public_url.raw} if self.public_url is not None else {}

    @property
    def resolved(self) -> dict[str, str]:
        hand = self.flattened
        references = {name: set(PLACEHOLDER.findall(value)) & hand.keys() for name, value in hand.items()}
        order, _blocked = peel_references(references)
        values = dict(self.minted)
        for name in order:
            values[name] = resolve_value(hand[name], values)
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
                    raise ValueError(
                        f"selected entry {name!r} has no bundle output {bundle_compose_path.as_posix()}; "
                        f"generate-score writes it"
                    )
                lock_references = lock.manifest_references(self.platform)
                tree_root, tree_builds = root, config.service_builds
            else:
                # Include an overridden ref through its local path
                candidate = self.path_overrides[name]
                bundle_compose_path = (candidate if candidate.is_absolute() else root / candidate).resolve()
                if not bundle_compose_path.is_file():
                    raise ValueError(f"path_overrides.{name}: {bundle_compose_path.as_posix()} does not exist")

                # The producer root sits four levels above a stack/generated/compose/ output
                producer_root = bundle_compose_path.parents[3] if len(bundle_compose_path.parents) > 3 else None
                if producer_root is None or not (producer_root / DEFAULT_CONFIG_PATH).is_file():
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
            scan = scan_bundle_texts(name, service.routes, service.ports, [text])
            if EDGE_SERVICE_NAME in scan.service_names:
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


def reference_cycle(blocked: Mapping[str, AbstractSet[str]]) -> list[str]:
    # Every blocked name keeps a blocked successor, so the walk repeats a node exactly on the cycle
    seen: dict[str, int] = {}
    chain: list[str] = []
    node = min(blocked)
    while node not in seen:
        seen[node] = len(chain)
        chain.append(node)
        node = min(blocked[node])
    return chain[seen[node] :]


def resolve_value(value: str, resolved: Mapping[str, str]) -> str:
    parts = PLACEHOLDER.split(value)
    return "".join(part if index % 2 == 0 else resolved[part] for index, part in enumerate(parts))
