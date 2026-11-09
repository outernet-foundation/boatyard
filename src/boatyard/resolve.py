from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from boatyard.models.config_file import (
    CADDY_IMAGE,
    EDGE_LISTEN_PORT,
    EDGE_SERVICE_NAME,
    OCI_REFERENCE_PREFIX,
    PLACEHOLDER,
    ConfigFile,
    FlattenedServiceEntry,
    Image,
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
    services: list[FlattenedServiceEntry]
    includes: list[str]
    env: dict[str, str]
    edge_image: str | None


class BundleSources(NamedTuple):
    bundle_path: Path
    lock_references: dict[str, str]
    tree_root: Path
    images: dict[str, Image]


class IncludedBundle(NamedTuple):
    row: FlattenedServiceEntry
    path: str
    text: str


def resolve(config: ConfigFile, lock: LockFile, values: ValuesFile) -> AssemblyPlan:
    # Combine the config, the lock, and the values file into the assembly plan that `up` renders
    source = values.source
    hand = values.values
    minted = _minted(config, values)
    consumer_manifests = lock.manifest_references(values.platform)
    service_entries = config.service_entries
    root = config.root

    if values.gateway is not None and config.gateway is None:
        raise ValueError(f"{source}: gateway is set but boatyard.yml declares no gateway")

    # Raise if a name in the bundles section of the values file is not a declared option of any service group
    qualified_options = {row.name: (row.root, row.option) for row in service_entries if row.option is not None}
    unknown = sorted(set(values.bundles) - set(qualified_options))
    if unknown:
        raise ValueError(f"{source}: bundles: names {unknown} are not among the selectable options")

    # Raise if a name in the ports section of the values file does not name a port-declaring entry
    port_declaring_names = {row.name for row in service_entries if row.effective_ports}
    unknown = sorted(set(values.ports) - port_declaring_names)
    if unknown:
        raise ValueError(f"{source}: ports: names {unknown} are not among the port-declaring entries")

    # Raise if a name in the path_overrides section of the values file does not name a ref entry
    ref_entry_names = {row.name for row in service_entries if row.entry.remote}
    unknown = sorted(set(values.path_overrides) - ref_entry_names)
    if unknown:
        raise ValueError(f"{source}: path_overrides: names {unknown} are not among the ref entries")

    # Fold the selection into one option per group, raising when two options of one group are selected
    choices: dict[str, str] = {}
    for name in values.bundles:
        group, option_name = qualified_options[name]
        if group in choices:
            raise ValueError(
                f"{source}: bundles: {name} and {qualified_option_name(group, choices[group])} "
                f"are two options of one group ({group})"
            )
        choices[group] = option_name

    # The selection's winners, and the overrides active on them
    winners = {row.name for row in service_entries if _in_assembly(row, choices)}
    overrides = {name for name in values.path_overrides if name in winners}

    slots_by_entry: dict[str, dict[str, Slot]] = {}
    texts: dict[str, str] = {}
    paths: dict[str, Path] = {}

    # Derive every declared entry's slot vocabulary.
    for row in service_entries:
        name = row.name

        # If a remote ref has no active override, its slots come from its lock record
        if row.entry.ref is not None and name not in overrides:
            locked = lock.bundles.get(bundle_lock_key(name))

            # If the lock records the bundle, its slots join the vocabulary
            if locked is not None:
                slots_by_entry[name] = dict(locked.slots)
            continue

        # If a local entry is declared, its slots come from the artifact generate-score rendered
        if row.entry.ref is None:
            bundle_path = compose_bundle_output_path(root, name).resolve()

            # If score generation never rendered the artifact, refuse
            if not bundle_path.is_file():
                raise ValueError(f"services entry {name!r} lacks its bundle output {bundle_path.as_posix()}")
            paths[name] = bundle_path
            texts[name] = bundle_path.read_text(encoding="utf-8")
            slots_by_entry[name] = artifact_slots([texts[name]])
            continue

        # If a winner's override is active, its slots come from the bundle its producer rendered
        candidate = values.path_overrides[name]
        override_path = candidate if candidate.is_absolute() else root / candidate

        # If the override points nowhere, refuse
        if not override_path.is_file():
            raise ValueError(f"path_overrides.{name}: {override_path.as_posix()} does not exist")
        bundle_path = override_path.resolve()
        paths[name] = bundle_path
        texts[name] = bundle_path.read_text(encoding="utf-8")
        slots_by_entry[name] = artifact_slots([texts[name]])

    includes: list[str] = []
    services: list[FlattenedServiceEntry] = []
    env: dict[str, str] = dict(minted)
    shas_by_root: dict[Path, dict[str, str]] = {}
    included_bundles: list[IncludedBundle] = []

    # Render each winner into includes, env fan entries, and bundle records.
    for row in service_entries:
        name = row.name

        # If the entry lost its group's selection, it renders nothing
        if name not in winners:
            continue

        services.append(row)

        # If a remote ref won without an active override, include its bundle by the recorded digest
        if row.entry.ref is not None and name not in overrides:
            locked = lock.require_bundle(bundle_lock_key(name), f"bundle {name!r}")
            includes.append(f"{OCI_REFERENCE_PREFIX}{locked.reference}")
            continue

        # If a local entry won, include the artifact this repo's score generation rendered
        if row.entry.ref is None:
            includes.append(paths[name].as_posix())
            sources = BundleSources(
                bundle_path=paths[name],
                lock_references=consumer_manifests,
                tree_root=root,
                images=config.images,
            )
            _entry_env(env, shas_by_root, name, sources)
            included_bundles.append(IncludedBundle(row=row, path=paths[name].as_posix(), text=texts[name]))
            continue

        # If a winner's override is active, include through the bundle its producer rendered

        # The producer root sits four levels above a stack/generated/compose/ output
        producer_root = paths[name].parents[3]

        # If no boatyard.yml-bearing repo root sits above the bundle, refuse
        if not (producer_root / DEFAULT_CONFIG_PATH).is_file():
            raise ValueError(f"{paths[name].as_posix()}: no boatyard.yml-bearing repo root above the bundle override")
        producer_config = ConfigFile.load(producer_root / DEFAULT_CONFIG_PATH)
        producer_lock = LockFile.load(producer_root / LOCK_PATH, producer_config)
        includes.append(paths[name].as_posix())
        sources = BundleSources(
            bundle_path=paths[name],
            lock_references=producer_lock.manifest_references(values.platform),
            tree_root=producer_root,
            images=producer_config.images,
        )
        _entry_env(env, shas_by_root, name, sources)
        included_bundles.append(IncludedBundle(row=row, path=paths[name].as_posix(), text=texts[name]))

    # Raise if an included bundle defines the reserved edge service or an unresolved route or port reference
    for bundle in included_bundles:
        name = bundle.row.name
        service_names = refuse_unresolved_references(
            name, bundle.row.effective_routes, bundle.row.effective_ports, [bundle.text], prefix=f"{name}-"
        )
        if EDGE_SERVICE_NAME in service_names:
            raise ValueError(f"{bundle.path}: the included bundle defines the reserved service {EDGE_SERVICE_NAME!r}")

    # Raise if no declared surface consumes a value from the values section
    consumers: dict[str, set[str]] = {}
    for entry_name, slots in slots_by_entry.items():
        for name in slots:
            consumers.setdefault(name, set()).add(entry_name)
    for name in sorted(hand):
        if name not in consumers:
            raise ValueError(f"{source}: {name} is consumed by no surface")

    # Raise if a reference in the values section resolves to neither a hand-set value nor a minted slot
    roots = set(hand) | set(minted)
    unresolvable = sorted({reference for referenced in values.references.values() for reference in referenced} - roots)
    if unresolvable:
        name = min(name for name, referenced in values.references.items() if referenced & set(unresolvable))
        raise ValueError(f"{source}: {name}: ${{{unresolvable[0]}}} resolves to no root")

    # Raise if no declared source fills a required slot of an assembled entry
    filled = set(hand) | set(minted)
    missing: dict[str, list[str]] = {}
    for entry_name in sorted(service.name for service in services):
        unfilled = sorted(
            name for name, slot in slots_by_entry.get(entry_name, {}).items() if slot.required and name not in filled
        )
        if unfilled:
            missing[entry_name] = unfilled
    if missing:
        entry_name, absent = min(missing.items())
        raise ValueError(f"{source}: {entry_name} requires {absent}, which no declared source fills")

    # Unfilled optionals ride as env-file empties; compose takes the :- default for set-to-empty
    # and unset alike
    for name in sorted({name for service in services for name in slots_by_entry.get(service.name, {})}):
        env.setdefault(name, "")
    for name in values.reference_order:
        parts = PLACEHOLDER.split(hand[name])
        env[name] = "".join(part if index % 2 == 0 else env[part] for index, part in enumerate(parts))

    return AssemblyPlan(
        services=services,
        includes=includes,
        env=env,
        edge_image=consumer_manifests.get(CADDY_IMAGE),
    )


def _minted(config: ConfigFile, values: ValuesFile) -> dict[str, str]:
    minted: dict[str, str] = {}
    if values.public_url is not None:
        minted[PUBLIC_URL_SLOT] = values.public_url.raw
    if config.gateway is not None:
        minted[EDGE_BASE_URL_SLOT] = f"http://{EDGE_SERVICE_NAME}:{EDGE_LISTEN_PORT}"
    return minted


def _in_assembly(row: FlattenedServiceEntry, choices: dict[str, str]) -> bool:
    return row.option is None or row.option == choices.get(row.root)


def _entry_env(
    env: dict[str, str], shas_by_root: dict[Path, dict[str, str]], name: str, sources: BundleSources
) -> None:
    env.update({env_lock_slot(name, key): value for key, value in sources.lock_references.items()})
    if sources.tree_root not in shas_by_root:
        shas_by_root[sources.tree_root] = {
            image.sha_env: image.compute_sha(sources.tree_root) for image in sources.images.values()
        }
    env.update({env_tree_slot(name, sha): value for sha, value in shas_by_root[sources.tree_root].items()})
