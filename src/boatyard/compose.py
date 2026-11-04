from __future__ import annotations

import shlex
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from itertools import combinations, groupby, starmap
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import NamedTuple

import yaml
from bashrun.bash import bash_output

from boatyard.config import (
    AUTH_GROUP_NAME,
    CADDY_IMAGE,
    ComposeWorkloadDocument,
    Config,
    DEV_TOGGLES_ANNOTATION,
    EDGE_SERVICE_NAME,
    OCI_REFERENCE_PREFIX,
    PortDeclaration,
    RouteConfig,
    Yaml,
    bundle_lock_key,
    compose_bundle_output_path,
    double_dollars,
    is_mapping,
    load_yaml_file,
    parse_dev_toggles,
    qualified_option_name,
    require_bundle_reference,
)
from boatyard.config_lock import LOCK_PATH, parse_config_lock, read_config_lock
from boatyard.environment import BUNDLES_VARIABLE, BUNDLE_OVERRIDE_PREFIX, ENV_FILE, Environment
from boatyard.lock import compose_env_entries, producer_env_entries, render_env_entries

EDGE_LISTEN_PORT = 8443
EDGE_CONFIG_NAME = "devkit-edge-caddyfile"

OVERRIDE_LAYOUT = ("stack", "generated", "compose")


class BundleOverride(NamedTuple):
    name: str
    path: Path | None


class Selection(NamedTuple):
    includes: list[str]
    routes: list[RouteConfig]
    ports: list[PortDeclaration]
    auth_route: RouteConfig | None
    forwarded: dict[str, str] | None
    profiles: list[str]
    dev_services: list[str]
    dev_toggles: dict[str, dict[str, str]]
    overrides: list[BundleOverride]


class Invocation(NamedTuple):
    compose_files: list[Path]
    lock_env_file: Path | None
    extra_env_files: list[Path]
    profiles: list[str]


def select(config: Config, environment: Environment) -> Selection:
    if not environment.bundles and not config.bare_entries and config.selectable_options:
        raise ValueError(
            f"{BUNDLES_VARIABLE}= selects nothing and no bare entry exists; "
            f"the options are {sorted(config.selectable_options)}"
        )
    unknown = [name for name in environment.bundles if name not in config.selectable_options]
    if unknown:
        bare_entries_note = f"; bare entries, always included: {config.bare_entries}" if config.bare_entries else ""
        raise ValueError(
            f"{BUNDLES_VARIABLE}= names {unknown}; "
            f"the selectable options are {sorted(config.selectable_options)}{bare_entries_note}"
        )
    choices: dict[str, str] = {}
    for name in environment.bundles:
        group, option_name = config.selectable_options[name]
        if group in choices:
            raise ValueError(
                f"{BUNDLES_VARIABLE}= selects {qualified_option_name(group, choices[group])} and {name}; "
                f"at most one option per group ({group})"
            )
        choices[group] = option_name

    unknown = sorted(
        name
        for name in environment.overrides
        if name not in config.selectable_options and name not in config.bare_entries
    )
    if unknown:
        vocabulary = sorted(config.selectable_options.keys() | set(config.bare_entries))
        raise ValueError(f"{BUNDLE_OVERRIDE_PREFIX}* names {unknown}; the bundle vocabulary is {vocabulary}")
    selected_names = set(starmap(qualified_option_name, choices.items())) | set(config.bare_entries)
    selected_overrides = {name: value for name, value in environment.overrides.items() if name in selected_names}

    auth_option = choices.get(AUTH_GROUP_NAME)

    profiles = environment.profiles
    forwarded = None
    if auth_option is not None:
        if environment.public_url is None:
            raise ValueError("an auth option is selected; PUBLIC_URL is required")
        forwarded = environment.public_url.forwarded

    services = config.resolve_services(choices)

    # Collect the include list in services declaration order; a remote ref includes the
    # lock's digest-pinned form, never the yaml tag directly. Each service answers one row:
    # its include string, an optional local output whose service names are scanned, and an
    # optional bundle override.
    lock = read_config_lock(config.root)
    includes: list[str] = []
    local_outputs: list[Path] = []
    overrides: list[BundleOverride] = []

    for service in [service for service in services if service.name in selected_overrides]:
        name = service.name
        value = selected_overrides[name]
        variable = BUNDLE_OVERRIDE_PREFIX + name.upper().replace("-", "_")

        if value.startswith(OCI_REFERENCE_PREFIX):
            require_bundle_reference(value)
            includes.append(value)
            overrides.append(BundleOverride(name=name, path=None))
            continue

        candidate = Path(value)
        path = (candidate if candidate.is_absolute() else config.root / candidate).resolve()
        if path.parts[-4:-1] != OVERRIDE_LAYOUT:
            raise ValueError(f"{variable}: {value!r} must point into a stack/generated/compose/ bundle output layout")
        if not path.is_file():
            raise ValueError(f"{variable}: {path.as_posix()} does not exist")
        loaded = load_yaml_file(path)
        document_services = loaded.get("services") if is_mapping(loaded) else None
        if is_mapping(document_services) and EDGE_SERVICE_NAME in document_services:
            raise ValueError(
                f"{path.as_posix()}: the bundle override defines the reserved service {EDGE_SERVICE_NAME!r}"
            )
        includes.append(path.as_posix())
        local_outputs.append(path)
        overrides.append(BundleOverride(name=name, path=path))

        continue

    for service in [service for service in services if service.name not in selected_overrides]:
        name = service.name
        if service.entry.ref is not None:
            pinned = lock.require(bundle_lock_key(name), f"bundle {name!r}", hint="run boatyard lock")
            includes.append(f"{OCI_REFERENCE_PREFIX}{pinned}")

            continue

        output = compose_bundle_output_path(config.root, name)
        if not output.is_file():
            raise ValueError(
                f"selected entry {name!r} has no bundle output {output.as_posix()}; generate-score writes it"
            )
        includes.append(output.resolve().as_posix())
        local_outputs.append(output)

    # Refuse selected outputs that define the same service name.
    file_services: list[tuple[Path, set[str]]] = []
    for path in local_outputs:
        # Extract the service names only.
        loaded = load_yaml_file(path)
        document_services = loaded.get("services") if is_mapping(loaded) else None
        file_services.append((path, set(document_services) if is_mapping(document_services) else set()))
    for (path, service_names), (other_path, other_service_names) in combinations(file_services, 2):
        shared = sorted(service_names & other_service_names)
        if shared:
            raise ValueError(f"{path.as_posix()} and {other_path.as_posix()} both define service(s) {shared}")
    output_services = {name for _path, names in file_services for name in names}

    # Dev facts: dev-stage services inside the selection, plus the workload-declared dev toggles.
    dev_services = [
        name for name, service in config.service_builds.items() if service.dev_stage and name in output_services
    ]
    dev_toggles: dict[str, dict[str, str]] = {}
    score_dir = config.root / config.score_dir
    for service in services:
        if service.entry.remote:
            continue
        for workload in service.entry.workloads:
            annotations = ComposeWorkloadDocument.load(score_dir / workload).metadata.annotations
            raw = annotations.get(DEV_TOGGLES_ANNOTATION)
            stem = Path(workload).stem
            if raw is None or stem not in output_services:
                continue
            dev_toggles[stem] = parse_dev_toggles(raw)

    return Selection(
        includes=includes,
        routes=[route for service in services for route in service.routes],
        ports=[port for service in services for port in service.ports],
        auth_route=None
        if auth_option is None
        else next(
            service for service in services if service.name == qualified_option_name(AUTH_GROUP_NAME, auth_option)
        ).routes[0],
        forwarded=forwarded,
        profiles=profiles,
        dev_services=dev_services,
        dev_toggles=dev_toggles,
        overrides=overrides,
    )


def render(
    config: Config,
    platform: str | None,
    selection: Selection,
    stack: ExitStack,
    dev: bool,
) -> Invocation:
    # Materialize the per-run files.
    lock_env_file = write_lock_env_file(config, platform, stack)
    producer_env_files = write_producer_env_files(selection.overrides, platform, stack)
    compose_files: list[Path] = []

    # Render the per-run root compose file.
    services: dict[str, Yaml] = {}
    if selection.routes:
        if config.gateway is None:
            raise ValueError("routes are declared; gateway is required")
        edge: dict[str, Yaml] = {
            "image": f"${{{CADDY_IMAGE}:?err}}",
            "configs": [{"source": EDGE_CONFIG_NAME, "target": "/etc/caddy/Caddyfile"}],
            "ports": [{"target": str(EDGE_LISTEN_PORT), "published": config.gateway, "protocol": "tcp"}],
        }
        if selection.forwarded is not None:
            edge["environment"] = {key: value for key, value in selection.forwarded.items()}
        services[EDGE_SERVICE_NAME] = edge
    ordered_ports = sorted(selection.ports, key=lambda port: port.service)
    port_services: dict[str, Yaml] = {
        service: {
            "ports": [
                {
                    "target": port.target,
                    "published": port.published,
                    "protocol": port.protocol,
                    **({"host_ip": port.host_ip} if port.host_ip is not None else {}),
                }
                for port in service_ports
            ]
        }
        for service, service_ports in groupby(ordered_ports, key=lambda port: port.service)
    }
    services.update(port_services)
    document: dict[str, Yaml] = {"name": config.project_name}
    if selection.includes:
        document["include"] = [include for include in selection.includes]
    if services:
        document["services"] = services
    if selection.routes:
        # Render one site with one handle block per route in declaration order.
        lines = [f":{EDGE_LISTEN_PORT} {{"]
        for route in selection.routes:
            matcher = "" if route.path == "/*" else f" {route.path}"
            if route.raw is not None:
                block = ["\t\t" + line for line in route.raw.splitlines()]
            elif route is selection.auth_route:
                block = [
                    f"\t\treverse_proxy {route.upstream} {{",
                    "\t\t\theader_up X-Forwarded-Proto {$FORWARDED_PROTO}",
                    "\t\t\theader_up X-Forwarded-Port {$FORWARDED_PORT}",
                    "\t\t}",
                ]
            else:
                block = [f"\t\treverse_proxy {route.upstream}"]
            head = "\thandle_path" if route.strip_prefix else "\thandle"
            lines.extend((f"{head}{matcher} {{", *block, "\t}"))
        lines.append("}")
        document["configs"] = {
            EDGE_CONFIG_NAME: {"content": double_dollars("\n".join(lines) + "\n")},
        }
    compose_files.append(write_temp_compose_file(stack, document, "devkit-assembly-"))

    if dev:
        # Render the dev overlay.
        overlay_services: dict[str, Yaml] = {}
        for name in selection.dev_services:
            overlay_services[name] = {
                "image": f"{config.repository}/{name}:dev",
                "volumes": [
                    {
                        "type": "bind",
                        "source": config.root.resolve().as_posix(),
                        "target": "/app",
                        "read_only": True,
                    }
                ],
            }
        for service_name, toggles in selection.dev_toggles.items():
            service = overlay_services.get(service_name)
            if not is_mapping(service):
                service = {}
                overlay_services[service_name] = service
            service["environment"] = {key: f"${{{key}:-{value}}}" for key, value in toggles.items()}
        if not overlay_services:
            raise ValueError(
                f"--dev requires a dev shape: a service Dockerfile declaring a dev stage "
                f"or a workload carrying {DEV_TOGGLES_ANNOTATION}"
            )
        compose_files.append(write_temp_compose_file(stack, {"services": overlay_services}, "devkit-dev-overlay-"))

    if selection.routes or selection.ports:
        # Validate every route upstream and port owner against compose's own resolution.
        resolved = set(
            _compose_config(
                config.root,
                compose_files,
                "--services",
                lock_env_file=lock_env_file,
                extra_env_files=producer_env_files,
            )
        )
        for route in selection.routes:
            upstream = route.upstream.partition(":")[0]
            if upstream not in resolved:
                raise ValueError(
                    f"route {route.path} upstream {upstream} resolves to no service in the selected assembly"
                )
        for port in selection.ports:
            if port.service not in resolved:
                raise ValueError(f"ports for service {port.service!r} resolve to no service in the selected assembly")
    if selection.profiles:
        # Refuse profile names the compose files do not define.
        defined = _compose_config(
            config.root, compose_files, "--profiles", lock_env_file=lock_env_file, extra_env_files=producer_env_files
        )
        unknown = [profile for profile in selection.profiles if profile not in defined]
        if unknown:
            raise ValueError(
                f"COMPOSE_PROFILES selects unknown profile(s) {unknown}; "
                f"profiles defined by the compose files: {defined}"
            )

    return Invocation(
        compose_files=compose_files,
        lock_env_file=lock_env_file,
        extra_env_files=producer_env_files,
        profiles=selection.profiles,
    )


def require_env_file(root: Path) -> None:
    if not (root / ENV_FILE).exists():
        raise ValueError(".env is required")


def _compose_config(
    root: Path,
    compose_files: list[Path],
    *arguments: str,
    lock_env_file: Path | None,
    extra_env_files: list[Path] | None,
) -> list[str]:
    return [
        line.strip()
        for line in bash_output(
            compose_command(
                root, compose_files, "config", *arguments, lock_env_file=lock_env_file, extra_env_files=extra_env_files
            )
        ).splitlines()
        if line.strip()
    ]


def compose_command(
    root: Path,
    compose_files: list[Path],
    *arguments: str,
    lock_env_file: Path | None = None,
    extra_env_files: list[Path] | None = None,
) -> str:
    quoted_arguments = [shlex.quote(argument) for argument in arguments]
    files_arguments = " ".join(f"-f {shlex.quote(str(compose_file))}" for compose_file in compose_files)
    # Later --env-file entries win shared keys (pinned against the compose binary in tests), so
    # producer lock env files attach before the consumer's own lock env file.
    extra_arguments = "".join(f" --env-file {shlex.quote(str(extra))}" for extra in extra_env_files or [])
    lock_argument = f" --env-file {shlex.quote(str(lock_env_file))}" if lock_env_file is not None else ""
    prefix = (
        f"docker compose {files_arguments} --project-directory {shlex.quote(str(root))} "
        f"--env-file .env{extra_arguments}{lock_argument}"
    )
    return " ".join([prefix, *quoted_arguments])


@contextmanager
def lock_env_unwind(path: Path) -> Generator[None]:
    try:
        yield
    except BaseException:
        print(f"lock env file kept for inspection: {path}")
        print(path.read_text(encoding="utf-8"), end="")
        raise

    path.unlink(missing_ok=True)


def write_env_file(entries: Mapping[str, str], prefix: str, stack: ExitStack) -> Path:
    with NamedTemporaryFile("w", suffix=".env", prefix=prefix, delete=False, encoding="utf-8") as handle:
        handle.write(render_env_entries(entries))
        path = Path(handle.name)
    stack.enter_context(lock_env_unwind(path))
    return path


def write_lock_env_file(config: Config, platform: str | None, stack: ExitStack) -> Path | None:
    lock_path = config.root / LOCK_PATH
    if not lock_path.is_file():
        return None
    entries = compose_env_entries(parse_config_lock(lock_path), config, platform)
    if not entries:
        # A bundles-only lock carries nothing the compose environment consumes.
        return None
    return write_env_file(entries, "devkit-lock-env-", stack)


def write_producer_env_files(overrides: list[BundleOverride], platform: str | None, stack: ExitStack) -> list[Path]:
    # A path-form override's bundle output interpolates its producer's third-party holes; the
    # producer's committed lock fills them, derived from the fixed output layout three parents up.
    files: list[Path] = []
    for override in overrides:
        if override.path is None:
            continue
        producer_lock = override.path.parents[3] / LOCK_PATH
        if not producer_lock.is_file():
            continue
        entries = producer_env_entries(parse_config_lock(producer_lock), platform)
        if not entries:
            continue
        files.append(write_env_file(entries, "devkit-producer-env-", stack))
    return files


def write_temp_compose_file(stack: ExitStack, document: Mapping[str, Yaml], prefix: str) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix=prefix, delete=False, encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        path = Path(handle.name)
    stack.callback(path.unlink, missing_ok=True)
    return path
