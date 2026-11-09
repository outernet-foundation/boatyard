from __future__ import annotations

from collections.abc import Mapping, Set as AbstractSet
from pathlib import Path
from typing import NamedTuple

from boatyard.models.config_file import (
    AUTH_GROUP_NAME,
    EDGE_LISTEN_PORT,
    EDGE_SERVICE_NAME,
    OCI_REFERENCE_PREFIX,
    PLACEHOLDER,
    RESERVED_SLOT_MARKERS,
    ConfigFile,
    ResolvedService,
    Slot,
    artifact_slots,
    bundle_lock_key,
    compose_bundle_output_path,
    env_lock_slot,
    env_tree_slot,
    qualified_option_name,
    refuse_unresolved_references,
)
from boatyard.models.lock_file import LockFile
from boatyard.models.values_file import (
    EDGE_BASE_URL_SLOT,
    PUBLIC_URL_SLOT,
    ValuesFile,
)
from boatyard.paths import DEFAULT_CONFIG_PATH, LOCK_PATH


class AssemblyPlan(NamedTuple):
    services: list[ResolvedService]
    includes: list[str]
    values: dict[str, str]
    lock_entries: dict[str, str]
    tree_entries: dict[str, str]
    consumed: list[str]
    consumer_entries: dict[str, str]


def resolve(config: ConfigFile, lock: LockFile, values: ValuesFile) -> AssemblyPlan:
    # Fold the three files into the plan `up` renders
    source = values.source
    hand = values.flattened
    minted = _minted(config, values)
    consumer_manifests = lock.manifest_references(values.platform)
    members = config.members

    if values.gateway is not None and config.gateway is None:
        raise ValueError(f"{source}: gateway is set but boatyard.yml declares no gateway")

    # Refuse values-file names outside their computed vocabularies
    selectable = {row.name: (row.root, row.option) for row in members if row.option is not None}
    local_names = {row.name for row in members if not row.entry.remote}
    remote_names = {row.name for row in members if row.entry.remote}
    port_vocabulary = {row.name for row in members if row.effective_ports}
    for site, names, vocabulary, noun in (
        ("bundles", values.bundles, set(selectable), "selectable options"),
        ("ports", values.ports, port_vocabulary, "port-declaring entries"),
        ("path_overrides", values.path_overrides, remote_names, "ref entries"),
        ("values.images", values.values.images, local_names, "local entries"),
        ("values.bundles", values.values.bundles, remote_names, "ref entries"),
    ):
        unknown = sorted(set(names) - vocabulary)
        if unknown:
            raise ValueError(f"{source}: {site}: names {unknown} are not among the {noun}")

    # Select one option per group and ride the bare entries
    choices: dict[str, str] = {}
    for name in values.bundles:
        group, option_name = selectable[name]
        if group in choices:
            raise ValueError(
                f"{source}: bundles: {name} and {qualified_option_name(group, choices[group])} "
                f"are two options of one group ({group})"
            )
        choices[group] = option_name
    services = [
        ResolvedService(
            name=row.name,
            entry=row.entry,
            routes=row.effective_routes,
            ports=row.effective_ports,
            auth=row.root == AUTH_GROUP_NAME,
        )
        for row in members
        if row.option is None or row.option == choices.get(row.root)
    ]
    selected = {service.name: service for service in services}
    participating = set(selected)

    # Derive every entry's surface, include, and fans in one walk
    root = config.root
    includes: list[str] = []
    lock_entries: dict[str, str] = {}
    tree_entries: dict[str, str] = {}
    slots_by_unit: dict[str, dict[str, Slot]] = {}
    shas_by_root: dict[Path, dict[str, str]] = {}
    for row in members:
        name = row.name
        service = selected.get(name)

        if row.entry.ref is not None:
            # Take a remote ref's surface from its lock record
            lock_key = bundle_lock_key(name)
            record = lock.bundles.get(lock_key)
            if record is not None:
                slots_by_unit[name] = dict(record.slots)
            if name not in values.path_overrides:
                if service is not None:
                    # Include a pinned ref by its lock digest
                    pinned = lock.require(lock_key, f"bundle {name!r}")
                    includes.append(f"{OCI_REFERENCE_PREFIX}{pinned}")
                continue
            if service is None:
                continue
            # Include an overridden ref through its producer repo
            bundle_path = _override_bundle_path(values, name, root).resolve()
            producer_root = bundle_path.parents[3]
            if not (producer_root / DEFAULT_CONFIG_PATH).is_file():
                raise ValueError(
                    f"{bundle_path.as_posix()}: no boatyard.yml-bearing repo root above the bundle override"
                )
            producer_config = ConfigFile.load(producer_root / DEFAULT_CONFIG_PATH)
            producer_lock = LockFile.load(producer_root / LOCK_PATH, producer_config)
            lock_references = producer_lock.manifest_references(values.platform)
            tree_root, tree_images = producer_root, producer_config.images
        else:
            # Include a local entry through its rendered bundle artifact
            bundle_path = compose_bundle_output_path(root, name).resolve()
            if not bundle_path.is_file():
                raise ValueError(f"services entry {name!r} lacks its bundle output {bundle_path.as_posix()}")
            lock_references = consumer_manifests
            tree_root, tree_images = root, config.images

        text = bundle_path.read_text(encoding="utf-8")
        slots_by_unit[name] = artifact_slots([text])
        if service is None:
            continue
        # Refuse reserved services and unresolved route/port references
        service_names = refuse_unresolved_references(name, service.routes, service.ports, [text], prefix=f"{name}-")
        if EDGE_SERVICE_NAME in service_names:
            raise ValueError(
                f"{bundle_path.as_posix()}: the included bundle defines the reserved service {EDGE_SERVICE_NAME!r}"
            )
        includes.append(bundle_path.as_posix())
        lock_entries.update({env_lock_slot(name, key): value for key, value in lock_references.items()})
        if tree_root not in shas_by_root:
            shas_by_root[tree_root] = {image.sha_env: image.compute_sha(tree_root) for image in tree_images.values()}
        tree_entries.update({env_tree_slot(name, sha): value for sha, value in shas_by_root[tree_root].items()})

    # Refuse values keys consumed by no surface or beyond their unit
    consumers: dict[str, set[str]] = {}
    for unit, slots in slots_by_unit.items():
        for name in slots:
            consumers.setdefault(name, set()).add(unit)
    for site, entries in (("values.images", values.values.images), ("values.bundles", values.values.bundles)):
        for name in sorted(entries):
            for key in sorted(entries[name]):
                if key not in slots_by_unit.get(name, {}):
                    raise ValueError(f"{source}: {site}.{name}: {key} is consumed by no surface of {name}")
                if consumers[key] - {name}:
                    raise ValueError(
                        f"{source}: {site}.{name}: {key} is consumed by {', '.join(sorted(consumers[key]))}, "
                        f"not {name} alone"
                    )
    for name in sorted(values.values.shared):
        if name not in consumers:
            raise ValueError(f"{source}: values.shared: {name} is consumed by no surface")
        if len(consumers[name]) == 1:
            [home] = consumers[name]
            raise ValueError(f"{source}: values.shared: {name} is consumed by {home} only")

    # Refuse non-bare references, unresolvable roots, and cycles
    sections: dict[str, Mapping[str, str]] = {"values.shared": values.values.shared}
    sections.update((f"values.images.{unit}", section) for unit, section in values.values.images.items())
    sections.update((f"values.bundles.{unit}", section) for unit, section in values.values.bundles.items())
    references: dict[str, set[str]] = {}
    sites: dict[str, str] = {}
    for site, section in sorted(sections.items()):
        for name, value in section.items():
            if "${" in PLACEHOLDER.sub("", value):
                raise ValueError(f"{source}: {site}: {name}: only bare ${{NAME}} references are legal")
            for reference in PLACEHOLDER.findall(value):
                if any(marker in reference for marker in RESERVED_SLOT_MARKERS):
                    raise ValueError(f"{source}: {site}: {name}: ${{{reference}}} must not carry a reserved marker")
                references.setdefault(name, set()).add(reference)
                sites[name] = site
    roots = set(hand) | set(minted)
    unresolvable = sorted({reference for referenced in references.values() for reference in referenced} - roots)
    if unresolvable:
        name = min(name for name, referenced in references.items() if referenced & set(unresolvable))
        raise ValueError(f"{source}: {sites[name]}: {name}: ${{{unresolvable[0]}}} resolves to no root")
    hand_references = {
        name: {reference for reference in PLACEHOLDER.findall(value) if reference in hand}
        for name, value in hand.items()
    }
    order, blocked = _peel_references(hand_references)
    if blocked:
        cycle_nodes = {name: hand_references[name] & set(blocked) for name in blocked}
        # Follow blocked successors until a node repeats
        seen: dict[str, int] = {}
        chain: list[str] = []
        node = min(cycle_nodes)
        while node not in seen:
            seen[node] = len(chain)
            chain.append(node)
            node = min(cycle_nodes[node])
        cycle = chain[seen[node] :]
        raise ValueError(f"{source}: reference cycle: {' -> '.join([*cycle, cycle[0]])}")

    # Refuse required slots no declared source fills
    filled = set(hand) | set(minted)
    missing: dict[str, list[str]] = {}
    for unit in sorted(participating):
        unfilled = sorted(
            name for name, slot in slots_by_unit.get(unit, {}).items() if slot.required and name not in filled
        )
        if unfilled:
            missing[unit] = unfilled
    if missing:
        unit, absent = min(missing.items())
        home = "images" if unit in local_names else "bundles"
        raise ValueError(f"{source}: values.{home}.{unit} requires {absent}, which no declared source fills")

    consumed = sorted({name for unit in participating for name in slots_by_unit.get(unit, {})})
    resolved_values = dict(minted)
    for name in order:
        parts = PLACEHOLDER.split(hand[name])
        resolved_values[name] = "".join(
            part if index % 2 == 0 else resolved_values[part] for index, part in enumerate(parts)
        )

    return AssemblyPlan(
        services=services,
        includes=includes,
        values=resolved_values,
        lock_entries=lock_entries,
        tree_entries=tree_entries,
        consumed=consumed,
        consumer_entries=consumer_manifests,
    )


def _override_bundle_path(values: ValuesFile, name: str, root: Path) -> Path:
    candidate = values.path_overrides[name]
    path = candidate if candidate.is_absolute() else root / candidate
    if not path.is_file():
        raise ValueError(f"path_overrides.{name}: {path.as_posix()} does not exist")
    return path


def _minted(config: ConfigFile, values: ValuesFile) -> dict[str, str]:
    minted: dict[str, str] = {}
    if values.public_url is not None:
        minted[PUBLIC_URL_SLOT] = values.public_url.raw
    if config.gateway is not None:
        minted[EDGE_BASE_URL_SLOT] = f"http://{EDGE_SERVICE_NAME}:{EDGE_LISTEN_PORT}"
    return minted


def _peel_references(references: Mapping[str, AbstractSet[str]]) -> tuple[list[str], list[str]]:
    # Answer the peel order and the cycle-blocked names
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
