from __future__ import annotations

import os
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
    ServiceGroup,
    Slot,
    artifact_slots,
    bundle_lock_key,
    compose_bundle_output_path,
    env_lock_slot,
    env_tree_slot,
    flatten_service_members,
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
    # One deterministic, offline fold of the three files into the plan `up` renders: selection,
    # demand tables, the values refusals, the per-entry walk, and the platform manifest table.
    source = values.source

    # Selection: qualified options one-per-group; bare entries ride unconditionally.
    members = flatten_service_members(config.services)
    selectable = {row.name: (row.root, row.option) for row in members if row.option is not None}
    bare = [row.root for row in members if row.option is None]
    unknown_bundles = [name for name in values.bundles if name not in selectable]
    if unknown_bundles:
        message = f"bundles: names {unknown_bundles} are not among the selectable options {sorted(selectable)}"
        if bare:
            message += f" (bare entries, always included: {bare})"
        raise ValueError(message)
    choices: dict[str, str] = {}
    for name in values.bundles:
        group, option_name = selectable[name]
        if group in choices:
            raise ValueError(
                f"bundles: {name} and {qualified_option_name(group, choices[group])} "
                f"are two options of one group ({group})"
            )
        choices[group] = option_name

    services: list[ResolvedService] = []
    for name, member in config.services.items():
        if isinstance(member, ServiceGroup):
            option_name = choices.get(name)
            if option_name is None:
                continue
            option = member.options[option_name]
            services.append(
                ResolvedService(
                    name=qualified_option_name(name, option_name),
                    entry=option,
                    routes=option.routes + member.routes,
                    ports=option.ports + member.ports,
                    auth=name == AUTH_GROUP_NAME,
                )
            )
        else:
            services.append(ResolvedService(name=name, entry=member, routes=member.routes, ports=member.ports))
    participating = {service.name for service in services}

    # Typed overrides name port-declaring entries; options inherit their group's declarations.
    port_vocabulary: list[str] = []
    for row in members:
        group_member = config.services[row.root]
        group_ports = group_member.ports if isinstance(group_member, ServiceGroup) else []
        if row.entry.ports or group_ports:
            port_vocabulary.append(row.name)
    unknown_ports = sorted(set(values.ports) - set(port_vocabulary))
    if unknown_ports:
        raise ValueError(
            f"{source}: ports: names {unknown_ports} are not among the port-declaring entries {sorted(port_vocabulary)}"
        )
    if values.gateway is not None and config.gateway is None:
        raise ValueError(f"{source}: gateway: is set but boatyard.yml declares no gateway")

    # Demand tables: authored consumer surfaces, lock-recorded bundle slots, override texts.
    slots_by_unit: dict[str, dict[str, Slot]] = dict(config.consumer_surfaces)
    homes = dict.fromkeys(config.consumer_surfaces, "images")
    remote_names = {name for name, _reference, _entry in config.remote_declared_entries()}
    for name in remote_names:
        homes[name] = "bundles"
        record = lock.bundles.get(bundle_lock_key(name))
        if record is not None:
            slots_by_unit[name] = dict(record.slots)
    for name in sorted(participating):
        if name not in values.path_overrides or name not in remote_names:
            continue
        slots_by_unit[name] = artifact_slots([
            _override_bundle_path(values, name, config.root).read_text(encoding="utf-8")
        ])

    # Values sections: every key sits in its consuming unit's section; shared keys are shared.
    local_names = sorted(name for name, home in homes.items() if home == "images")
    sorted_remote_names = sorted(name for name, home in homes.items() if home == "bundles")
    consumers: dict[str, set[str]] = {}
    for unit, slots in slots_by_unit.items():
        for name in slots:
            consumers.setdefault(name, set()).add(unit)

    for section_name, expected_home, noun, other_names in (
        ("images", "images", "local", local_names),
        ("bundles", "bundles", "ref", sorted_remote_names),
    ):
        entries = values.values.images if section_name == "images" else values.values.bundles
        for name in sorted(entries):
            if homes.get(name) != expected_home:
                raise ValueError(
                    f"{source}: values.{section_name}.{name} is not a {noun} entry "
                    f"(the {noun} entries are {other_names})"
                )
            site = f"values.{section_name}.{name}"
            surface = sorted(slots_by_unit.get(name, {}))
            for key in sorted(entries[name]):
                if key not in slots_by_unit.get(name, {}):
                    raise ValueError(f"{site}: {key} is consumed by no surface of {name} (the surface is {surface})")
                if consumers[key] - {name}:
                    raise ValueError(
                        f"{site}: {key} is consumed by {', '.join(sorted(consumers[key]))}, not {name} alone"
                    )

    for name in sorted(values.values.shared):
        if name not in consumers:
            raise ValueError(f"{source}: values.shared: {name} is consumed by no surface")
        if len(consumers[name]) == 1:
            [home] = consumers[name]
            raise ValueError(f"{source}: values.shared: {name} is consumed by {home} only")

    # The interpolation grammar: bare references only, roots resolvable, no cycles.
    sections: dict[str, Mapping[str, str]] = {"values.shared": values.values.shared}
    sections.update((f"values.images.{unit}", section) for unit, section in values.values.images.items())
    sections.update((f"values.bundles.{unit}", section) for unit, section in values.values.bundles.items())
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
    roots = set(values.flattened) | set(_minted(config, values))
    unresolvable = sorted({reference for referenced in references.values() for reference in referenced} - roots)
    if unresolvable:
        name = min(name for name, referenced in references.items() if referenced & set(unresolvable))
        raise ValueError(
            f"{source}: {sites[name]}: {name}: ${{{unresolvable[0]}}} resolves to no root "
            f"(the roots are {sorted(roots)})"
        )
    hand_edges = {
        name: {reference for reference in PLACEHOLDER.findall(value) if reference in values.flattened}
        for name, value in values.flattened.items()
    }
    _order, blocked = _peel_references(hand_edges)
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

    # Every required slot is filled by some declared source.
    filled = set(values.values.shared)
    for name, home_section in homes.items():
        if home_section == "images" and name in values.values.images:
            filled.update(values.values.images[name])
        elif name in values.values.bundles:
            filled.update(values.values.bundles[name])
    if values.public_url is not None:
        filled.add(PUBLIC_URL_SLOT)
    if config.gateway is not None:
        filled.add(EDGE_BASE_URL_SLOT)
    missing: dict[str, list[str]] = {}
    for unit in sorted(participating):
        unfilled = sorted(
            name for name, slot in slots_by_unit.get(unit, {}).items() if slot.required and name not in filled
        )
        if unfilled:
            missing[unit] = unfilled
    if missing:
        unit, absent = min(missing.items())
        ignored = sorted(shell_name for shell_name in absent if os.environ.get(shell_name))
        note = f" ({', '.join(ignored)} are set in the shell and ignored)" if ignored else ""
        raise ValueError(
            f"{source}: values.{homes[unit]}.{unit} requires {absent}, which no declared source fills{note}"
        )

    # path_overrides name vocabulary members; only ref entries are overridable.
    bundle_vocabulary = {row.name for row in members}
    unknown_overrides: list[str] = []
    local_override_names: list[str] = []
    for name in values.path_overrides:
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

    # The per-entry walk: includes, lock fans, tree fans.
    root = config.root
    lock_entries: dict[str, str] = {}
    tree_entries: dict[str, str] = {}
    shas_by_root: dict[Path, dict[str, str]] = {}
    includes: list[str] = []

    for service in services:
        name = service.name

        # Include a remote ref as its lock-pinned digest
        if service.entry.ref is not None and name not in values.path_overrides:
            pinned = lock.require(bundle_lock_key(name), f"bundle {name!r}")
            includes.append(f"{OCI_REFERENCE_PREFIX}{pinned}")
            continue

        if service.entry.ref is None:
            bundle_compose_path = compose_bundle_output_path(root, name)
            if not bundle_compose_path.is_file():
                raise ValueError(f"selected entry {name!r} lacks its bundle output {bundle_compose_path.as_posix()}")
            lock_references = lock.manifest_references(values.platform)
            tree_root, tree_images = root, config.images
        else:
            # Include an overridden ref through its local path
            bundle_compose_path = _override_bundle_path(values, name, root).resolve()
            # The producer root sits four levels above a stack/generated/compose/ output
            producer_root = bundle_compose_path.parents[3]
            if not (producer_root / DEFAULT_CONFIG_PATH).is_file():
                raise ValueError(
                    f"{bundle_compose_path.as_posix()}: no boatyard.yml-bearing repo root above the bundle override"
                )
            # Project the producer lock's manifest references under the consumer's platform
            producer_config = ConfigFile.load(producer_root / DEFAULT_CONFIG_PATH)
            producer_lock = LockFile.load(producer_root / LOCK_PATH, producer_config)
            lock_references = producer_lock.manifest_references(values.platform)
            tree_root, tree_images = producer_root, producer_config.images

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
            shas_by_root[tree_root] = {image.sha_env: image.compute_sha(tree_root) for image in tree_images.values()}
        tree_entries.update({env_tree_slot(name, sha): value for sha, value in shas_by_root[tree_root].items()})

    consumed = sorted({name for unit in participating for name in slots_by_unit.get(unit, {})})

    # Interpolation: minted slots first, then peeled hand values over bare references.
    hand = values.flattened
    hand_references = {name: set(PLACEHOLDER.findall(value)) & hand.keys() for name, value in hand.items()}
    order, _blocked = _peel_references(hand_references)
    resolved_values = dict(_minted(config, values))
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
        consumer_entries=lock.manifest_references(values.platform),
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
