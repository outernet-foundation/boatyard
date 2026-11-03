from __future__ import annotations

import shlex
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from itertools import combinations, groupby, starmap
from os import environ
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
    ServiceEntry,
    ServiceGroup,
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
from boatyard.lock import compose_env_entries, producer_env_entries, render_env_entries

ENV_FILE = Path(".env")
BUNDLES_VARIABLE = "BUNDLES"
BUNDLE_OVERRIDE_PREFIX = "BUNDLE_OVERRIDE_"

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


def select(config: Config, environment: Mapping[str, str]) -> Selection:
    names = split_names(environment.get(BUNDLES_VARIABLE, ""))
    root = config.root

    options = config.selectable_options
    bare_entries = config.bare_entries
    if not names and not bare_entries and options:
        raise ValueError(
            f"{BUNDLES_VARIABLE}= selects nothing and no bare entry exists; the options are {sorted(options)}"
        )
    unknown = [name for name in names if name not in options]
    if unknown:
        bare_entries_note = f"; bare entries, always included: {bare_entries}" if bare_entries else ""
        raise ValueError(
            f"{BUNDLES_VARIABLE}= names {unknown}; the selectable options are {sorted(options)}{bare_entries_note}"
        )
    choices: dict[str, str] = {}
    for name in names:
        group, option_name = options[name]
        if group in choices:
            raise ValueError(
                f"{BUNDLES_VARIABLE}= selects {qualified_option_name(group, choices[group])} and {name}; "
                f"at most one option per group ({group})"
            )
        choices[group] = option_name

    # Local bundle overrides ride .env per machine: BUNDLE_OVERRIDE_<QUALIFIED_NAME> = a path
    # into the fixed stack/generated/compose/ layout or an oci:// tag of any channel.
    raw_overrides = {
        key.removeprefix(BUNDLE_OVERRIDE_PREFIX).lower().replace("_", "-"): value
        for key, value in environment.items()
        if key.startswith(BUNDLE_OVERRIDE_PREFIX)
    }
    unknown = sorted(name for name in raw_overrides if name not in options and name not in bare_entries)
    if unknown:
        vocabulary = sorted(options.keys() | set(bare_entries))
        raise ValueError(f"{BUNDLE_OVERRIDE_PREFIX}* names {unknown}; the bundle vocabulary is {vocabulary}")
    selected_names = set(starmap(qualified_option_name, choices.items())) | set(bare_entries)
    selected_overrides = {name: value for name, value in raw_overrides.items() if name in selected_names}

    auth_option = choices.get(AUTH_GROUP_NAME)

    profiles = split_names(environment.get("COMPOSE_PROFILES", ""))
    forwarded = None
    if auth_option is not None:
        public_url = environment.get("PUBLIC_URL", "").strip()
        if not public_url:
            raise ValueError("an auth option is selected; PUBLIC_URL is required")
        scheme, _, rest = public_url.partition("://")
        hostport = rest.partition("/")[0]
        if ":" in hostport:
            forwarded_port = hostport.rpartition(":")[2]
        elif scheme == "https":
            forwarded_port = "443"
        else:
            forwarded_port = "80"
        forwarded = {"FORWARDED_PROTO": scheme, "FORWARDED_PORT": forwarded_port}

    # Flatten the selected entries in declaration order.
    entries: list[tuple[str, ServiceGroup | ServiceEntry]] = []
    for name, entry in config.services.items():
        if isinstance(entry, ServiceGroup):
            entries.append((name, entry))
            option_name = choices.get(name)
            if option_name is not None:
                entries.append((qualified_option_name(name, option_name), entry.options[option_name]))
        else:
            entries.append((name, entry))

    # Collect the include list in services declaration order; a remote ref includes the
    # lock's digest-pinned form, never the yaml tag directly.
    lock = read_config_lock(root)
    includes: list[str] = []
    local_outputs: list[Path] = []
    overrides: list[BundleOverride] = []
    for name, entry in entries:
        if isinstance(entry, ServiceGroup):
            continue
        if name in selected_overrides:
            variable = BUNDLE_OVERRIDE_PREFIX + name.upper().replace("-", "_")
            value = selected_overrides[name].strip()
            if not value:
                raise ValueError(f"{variable}: empty; a value is a filesystem path or an oci:// reference")
            if value.startswith(OCI_REFERENCE_PREFIX):
                require_bundle_reference(value)
                overrides.append(BundleOverride(name=name, path=None))
                includes.append(value)
                continue
            candidate = Path(value)
            path = (candidate if candidate.is_absolute() else root / candidate).resolve()
            if path.parts[-4:-1] != OVERRIDE_LAYOUT:
                raise ValueError(
                    f"{variable}: {value!r} must point into a stack/generated/compose/ bundle output layout"
                )
            if not path.is_file():
                raise ValueError(f"{variable}: {path.as_posix()} does not exist")
            overrides.append(BundleOverride(name=name, path=path))
            includes.append(path.as_posix())
            local_outputs.append(path)
            continue
        if entry.ref is not None:
            pinned = lock.require(bundle_lock_key(name), f"bundle {name!r}", hint="run boatyard lock")
            includes.append(f"{OCI_REFERENCE_PREFIX}{pinned}")
            continue
        output = compose_bundle_output_path(root, name)
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
    override_paths = {override.path for override in overrides if override.path is not None}
    for path, service_names in file_services:
        if path in override_paths and EDGE_SERVICE_NAME in service_names:
            raise ValueError(
                f"{path.as_posix()}: the bundle override defines the reserved service {EDGE_SERVICE_NAME!r}"
            )

    # Gather the routes, ports, and the selected auth route.
    routes = [route for _, entry in entries for route in entry.routes]
    ports = [port for _, entry in entries for port in entry.ports]
    auth_route = None
    auth = config.services.get(AUTH_GROUP_NAME)
    if isinstance(auth, ServiceGroup) and auth_option is not None:
        auth_route = auth.options[auth_option].routes[0]

    # Dev facts: dev-stage services inside the selection, plus the workload-declared dev toggles.
    dev_services = [
        name for name, service in config.service_builds.items() if service.dev_stage and name in output_services
    ]
    dev_toggles: dict[str, dict[str, str]] = {}
    score_dir = config.root / config.score_dir
    for _, entry in entries:
        if isinstance(entry, ServiceGroup) or entry.remote:
            continue
        for workload in entry.workloads:
            annotations = ComposeWorkloadDocument.load(score_dir / workload).metadata.annotations
            raw = annotations.get(DEV_TOGGLES_ANNOTATION)
            stem = Path(workload).stem
            if raw is None or stem not in output_services:
                continue
            dev_toggles[stem] = parse_dev_toggles(raw)

    return Selection(
        includes=includes,
        routes=routes,
        ports=ports,
        auth_route=auth_route,
        forwarded=forwarded,
        profiles=profiles,
        dev_services=dev_services,
        dev_toggles=dev_toggles,
        overrides=overrides,
    )


def render(
    config: Config,
    environment: Mapping[str, str],
    selection: Selection,
    stack: ExitStack,
    dev: bool,
) -> Invocation:
    # Materialize the per-run files.
    lock_env_file = write_lock_env_file(config, environment, stack)
    producer_env_files = write_producer_env_files(selection.overrides, environment, stack)
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
                config.root, compose_files, "--services", lock_env_file=lock_env_file, extra_env_files=producer_env_files
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


def merged_environment(root: Path) -> dict[str, str]:
    path = root / ENV_FILE
    environment: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            environment[key.strip()] = value.strip().strip("'\"")
    environment.update(environ)
    return environment


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


def write_lock_env_file(config: Config, environment: Mapping[str, str], stack: ExitStack) -> Path | None:
    lock_path = config.root / LOCK_PATH
    if not lock_path.is_file():
        return None
    entries = compose_env_entries(parse_config_lock(lock_path), config, environment)
    if not entries:
        # A bundles-only lock carries nothing the compose environment consumes.
        return None
    return write_env_file(entries, "devkit-lock-env-", stack)


def write_producer_env_files(
    overrides: list[BundleOverride], environment: Mapping[str, str], stack: ExitStack
) -> list[Path]:
    # A path-form override's bundle output interpolates its producer's third-party holes; the
    # producer's committed lock fills them, derived from the fixed output layout three parents up.
    files: list[Path] = []
    for override in overrides:
        if override.path is None:
            continue
        producer_lock = override.path.parents[3] / LOCK_PATH
        if not producer_lock.is_file():
            continue
        entries = producer_env_entries(parse_config_lock(producer_lock), environment)
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


def split_names(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
