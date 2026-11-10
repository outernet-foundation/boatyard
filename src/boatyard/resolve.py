from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from boatyard.lock import extract_bundle_records
from boatyard.models.config_file import (
    CADDY_IMAGE,
    EDGE_LISTEN_PORT,
    GATEWAY_SERVICE_NAME,
    PLACEHOLDER,
    ConfigFile,
    FlattenedServiceEntry,
    Image,
    PortDeclaration,
    RouteConfig,
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
    PUBLIC_HOST_SLOT,
    PUBLIC_PORT_SLOT,
    PUBLIC_SCHEME_SLOT,
    PUBLIC_SLOTS,
    PUBLIC_URL_PATH_SLOT,
    PUBLIC_URL_SLOT,
    ValuesFile,
)
from boatyard.paths import DEFAULT_CONFIG_PATH, LOCK_PATH


class BundleProject(NamedTuple):
    entry_name: str
    project_name: str
    bundle_path: Path | None
    pinned_reference: str | None
    services: list[str]
    effective_routes: list[RouteConfig]
    effective_ports: list[PortDeclaration]
    container_names: list[str]


class PublicFace(NamedTuple):
    host: str
    scheme: str
    port: int
    url_path: str


class GatewayPlan(NamedTuple):
    port: int
    public_face: PublicFace | None
    routes: list[tuple[str, RouteConfig]]


class AssemblyPlan(NamedTuple):
    bundles: list[BundleProject]
    gateway: GatewayPlan | None
    env: dict[str, str]
    edge_image: str | None


class BundleSources(NamedTuple):
    bundle_path: Path
    lock_references: dict[str, str]
    tree_root: Path
    images: dict[str, Image]


def resolve(config: ConfigFile, lock: LockFile, values: ValuesFile) -> AssemblyPlan:
    # Combine the config, the lock, and the values file into the assembly plan that `up` renders
    source = values.source
    hand = values.values
    public_face = _public_face(config, values)
    minted = _minted(config, values)
    consumer_manifests = lock.manifest_references(values.platform)
    service_entries = config.service_entries
    root = config.root

    if values.gateway_port is not None and config.gateway_port is None:
        raise ValueError(f"{source}: gateway_port is set but boatyard.yml declares no gateway")

    # Raise if a name in the bundles section of the values file is not a declared option of any service group
    qualified_options = {row.name: (row.root, row.option) for row in service_entries if row.option is not None}
    unknown = sorted(set(values.bundles) - set(qualified_options))
    if unknown:
        raise ValueError(f"{source}: bundles: names {unknown} are not among the selectable options")

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

    bundles: list[BundleProject] = []
    env: dict[str, str] = dict(minted)
    shas_by_root: dict[Path, dict[str, str]] = {}

    # Render each winner into a bundle project, adopting records and folding the env fans.
    for row in service_entries:
        name = row.name

        # If the entry lost its group's selection, it renders nothing
        if name not in winners:
            continue

        # If a remote ref won without an active override, its records come from the lock
        if row.entry.ref is not None and name not in overrides:
            locked = lock.require_bundle(bundle_lock_key(name), f"bundle {name!r}")
            adopted_services = list(locked.services)
            adopted_routes = list(locked.routes)
            adopted_ports = list(locked.ports)
            adopted_container_names = list(locked.container_names)
            bundle_path = None
            pinned_reference = locked.reference
        else:
            records = extract_bundle_records([texts[name]])
            adopted_services = records.services
            adopted_routes = records.routes
            adopted_ports = records.ports
            adopted_container_names = records.container_names
            bundle_path = paths[name]
            pinned_reference = None

        # Config-authored declarations replace the adopted set wholesale when non-empty
        effective_routes = list(row.effective_routes) if row.effective_routes else adopted_routes
        effective_ports = list(row.effective_ports) if row.effective_ports else adopted_ports

        bundles.append(
            BundleProject(
                entry_name=name,
                project_name=f"{config.project_name}-{name}",
                bundle_path=bundle_path,
                pinned_reference=pinned_reference,
                services=adopted_services,
                effective_routes=effective_routes,
                effective_ports=effective_ports,
                container_names=adopted_container_names,
            )
        )

        # The lock and tree fans ride for local and override entries only
        if bundle_path is not None:
            if row.entry.ref is None:
                sources = BundleSources(
                    bundle_path=bundle_path,
                    lock_references=consumer_manifests,
                    tree_root=root,
                    images=config.images,
                )
                _entry_env(env, shas_by_root, name, sources)
                continue

            # The producer root sits four levels above a stack/generated/compose/ output
            producer_root = bundle_path.parents[3]

            # If no boatyard.yml-bearing repo root sits above the bundle, refuse
            if not (producer_root / DEFAULT_CONFIG_PATH).is_file():
                raise ValueError(
                    f"{bundle_path.as_posix()}: no boatyard.yml-bearing repo root above the bundle override"
                )
            producer_config = ConfigFile.load(producer_root / DEFAULT_CONFIG_PATH)
            producer_lock = LockFile.load(producer_root / LOCK_PATH, producer_config)
            sources = BundleSources(
                bundle_path=bundle_path,
                lock_references=producer_lock.manifest_references(values.platform),
                tree_root=producer_root,
                images=producer_config.images,
            )
            _entry_env(env, shas_by_root, name, sources)

    # Raise if a local or override artifact defines the reserved gateway service or an unresolved reference
    for bundle in bundles:
        if bundle.bundle_path is None:
            continue
        service_names = refuse_unresolved_references(
            bundle.entry_name, bundle.effective_routes, bundle.effective_ports, [texts[bundle.entry_name]]
        )
        if GATEWAY_SERVICE_NAME in service_names:
            raise ValueError(
                f"{bundle.bundle_path.as_posix()}: the included bundle defines the reserved service "
                f"{GATEWAY_SERVICE_NAME!r}"
            )

    # Combined-set validations over every winner's effective declarations
    path_claims: dict[str, str] = {}
    for bundle in bundles:
        for route in bundle.effective_routes:
            if route.path in path_claims:
                raise ValueError(
                    f"{source}: route path {route.path} is claimed by both {path_claims[route.path]} "
                    f"and {bundle.entry_name}"
                )
            path_claims[route.path] = bundle.entry_name
    for row in service_entries:
        if row.name in winners and row.auth:
            selected = next(bundle for bundle in bundles if bundle.entry_name == row.name)
            if len(selected.effective_routes) != 1:
                raise ValueError(f"{source}: the selected auth option {row.name!r} carries exactly one effective route")
    if path_claims and config.gateway_port is None:
        raise ValueError(f"{source}: effective routes require a gateway declaration")

    # A values ports key names a service of a selected bundle carrying exactly one effective port
    services_by_project = {service: bundle for bundle in bundles for service in bundle.services}
    port_counts = {port.service: 0 for bundle in bundles for port in bundle.effective_ports}
    for bundle in bundles:
        for port in bundle.effective_ports:
            port_counts[port.service] += 1
    unknown = sorted(set(values.ports) - set(services_by_project))
    if unknown:
        raise ValueError(
            f"{source}: ports: names {unknown} are services of no selected bundle "
            f"(services: {sorted(services_by_project)})"
        )
    ambiguous = sorted(name for name in values.ports if port_counts.get(name, 0) != 1)
    if ambiguous:
        raise ValueError(f"{source}: ports: {ambiguous} carry a count of effective ports a scalar cannot disambiguate")

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

    # With no public face, no selected surface may consume one of the minted PUBLIC_* slots
    if public_face is None:
        for entry_name in sorted(bundle.entry_name for bundle in bundles):
            public_slots = sorted(name for name in slots_by_entry.get(entry_name, {}) if name in PUBLIC_SLOTS)
            if public_slots:
                raise ValueError(
                    f"{source}: {entry_name} consumes {public_slots[0]}, which requires public_host in the values file"
                )

    # Raise if no declared source fills a required slot of an assembled entry
    filled = set(hand) | set(minted)
    missing: dict[str, list[str]] = {}
    for entry_name in sorted(bundle.entry_name for bundle in bundles):
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
    for name in sorted({name for bundle in bundles for name in slots_by_entry.get(bundle.entry_name, {})}):
        env.setdefault(name, "")
    for name in values.reference_order:
        parts = PLACEHOLDER.split(hand[name])
        env[name] = "".join(part if index % 2 == 0 else env[part] for index, part in enumerate(parts))

    gateway = None
    if config.gateway_port is not None:
        routes = [(bundle.entry_name, route) for bundle in bundles for route in bundle.effective_routes]
        gateway = GatewayPlan(
            port=values.gateway_port if values.gateway_port is not None else config.gateway_port,
            public_face=public_face,
            routes=routes,
        )

    return AssemblyPlan(
        bundles=bundles,
        gateway=gateway,
        env=env,
        edge_image=consumer_manifests.get(CADDY_IMAGE),
    )


def _public_face(config: ConfigFile, values: ValuesFile) -> PublicFace | None:
    if values.public_host is None:
        return None
    scheme = values.public_scheme if values.public_scheme is not None else "http"
    effective_gateway = values.gateway_port if values.gateway_port is not None else config.gateway_port
    port = values.public_port
    if port is None:
        port = effective_gateway if effective_gateway is not None else (443 if scheme == "https" else 80)
    return PublicFace(
        host=values.public_host,
        scheme=scheme,
        port=port,
        url_path=values.public_url_path if values.public_url_path is not None else "",
    )


def _minted(config: ConfigFile, values: ValuesFile) -> dict[str, str]:
    minted: dict[str, str] = {}
    face = _public_face(config, values)
    if face is not None:
        scheme_default = 443 if face.scheme == "https" else 80
        spelled_port = "" if face.port == scheme_default else f":{face.port}"
        minted[PUBLIC_HOST_SLOT] = face.host
        minted[PUBLIC_SCHEME_SLOT] = face.scheme
        minted[PUBLIC_PORT_SLOT] = str(face.port)
        minted[PUBLIC_URL_PATH_SLOT] = face.url_path
        minted[PUBLIC_URL_SLOT] = f"{face.scheme}://{face.host}{spelled_port}{face.url_path}"
    if config.gateway_port is not None:
        minted[EDGE_BASE_URL_SLOT] = f"http://{GATEWAY_SERVICE_NAME}:{EDGE_LISTEN_PORT}"
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
