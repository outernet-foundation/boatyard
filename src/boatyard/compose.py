from __future__ import annotations

import shlex
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from itertools import groupby
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import NamedTuple

import yaml
from bashrun.bash import bash_output

from boatyard.config import (
    CADDY_IMAGE,
    Config,
    EDGE_SERVICE_NAME,
    OCI_REFERENCE_PREFIX,
    RouteConfig,
    Yaml,
    bundle_lock_key,
    compose_bundle_output_path,
    double_dollars,
    is_mapping,
    load_yaml_file,
    qualified_option_name,
)
from boatyard.config_lock import LOCK_PATH, parse_config_lock, read_config_lock
from boatyard.environment import BUNDLES_VARIABLE, BUNDLE_OVERRIDE_PREFIX, ENV_FILE, Environment
from boatyard.lock import compose_env_entries, producer_env_entries, render_env_entries

EDGE_LISTEN_PORT = 8443
EDGE_CONFIG_NAME = "devkit-edge-caddyfile"


class BundleOverride(NamedTuple):
    name: str
    path: Path | None


class Invocation(NamedTuple):
    compose_files: list[Path]
    env_file: Path | None
    profiles: list[str]
    overrides: list[BundleOverride]


def render_compose(config: Config, environment: Environment, stack: ExitStack) -> Invocation:
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
        for name in environment.oci_overrides.keys() | environment.path_overrides.keys()
        if name not in config.selectable_options and name not in config.bare_entries
    )
    if unknown:
        vocabulary = sorted(config.selectable_options.keys() | set(config.bare_entries))
        raise ValueError(f"{BUNDLE_OVERRIDE_PREFIX}* names {unknown}; the bundle vocabulary is {vocabulary}")

    services = config.resolve_services(choices)

    auth_service = next((service for service in services if service.auth), None)
    forwarded = None
    if auth_service is not None:
        if environment.public_url is None:
            raise ValueError("an auth option is selected; PUBLIC_URL is required")
        forwarded = environment.public_url.forwarded

    # Collect the include list in services declaration order; a remote ref includes the
    # lock's digest-pinned form, never the yaml tag directly. Each service answers one row:
    # its include string and an optional bundle override.
    lock = read_config_lock(config.root)
    includes: list[str] = []
    overrides: list[BundleOverride] = []

    for service in services:
        name = service.name

        if name in environment.oci_overrides:
            includes.append(environment.oci_overrides[name])
            overrides.append(BundleOverride(name=name, path=None))
            continue

        if name in environment.path_overrides:
            candidate = environment.path_overrides[name]
            path = (candidate if candidate.is_absolute() else config.root / candidate).resolve()
            variable = BUNDLE_OVERRIDE_PREFIX + name.upper().replace("-", "_")
            if not path.is_file():
                raise ValueError(f"{variable}: {path.as_posix()} does not exist")
            loaded = load_yaml_file(path)
            loaded_services = loaded.get("services") if is_mapping(loaded) else None
            if is_mapping(loaded_services) and EDGE_SERVICE_NAME in loaded_services:
                raise ValueError(
                    f"{path.as_posix()}: the bundle override defines the reserved service {EDGE_SERVICE_NAME!r}"
                )
            includes.append(path.as_posix())
            overrides.append(BundleOverride(name=name, path=path))
            continue

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

    routes = [namespaced_route(service.name, route) for service in services for route in service.routes]
    ports = [
        port.model_copy(update={"service": f"{service.name}-{port.service}"})
        for service in services
        for port in service.ports
    ]
    # Pick the auth route from the flattened list so the caddyfile's identity check sees the same copy.
    auth_route = (
        None if auth_service is None else next(route for route in routes if route.path == auth_service.routes[0].path)
    )
    profiles = environment.profiles

    # Materialize the per-run files.
    env_file = write_assembly_env_file(config, overrides, environment.platform, stack)
    compose_files: list[Path] = []

    # Render the per-run root compose file.
    document_services: dict[str, Yaml] = {}
    if routes:
        if config.gateway is None:
            raise ValueError("routes are declared; gateway is required")
        edge: dict[str, Yaml] = {
            "image": f"${{{CADDY_IMAGE}:?err}}",
            "configs": [{"source": EDGE_CONFIG_NAME, "target": "/etc/caddy/Caddyfile"}],
            "ports": [{"target": str(EDGE_LISTEN_PORT), "published": config.gateway, "protocol": "tcp"}],
            # Attach the edge to every selected bundle's network.
            "networks": dict.fromkeys(f"{service.name}-net" for service in services),
        }
        if forwarded is not None:
            edge["environment"] = {key: value for key, value in forwarded.items()}
        document_services[EDGE_SERVICE_NAME] = edge
    document_services.update(
        {
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
            for service, service_ports in groupby(
                sorted(ports, key=lambda port: port.service), key=lambda port: port.service
            )
        }
    )
    document: dict[str, Yaml] = {"name": config.project_name}
    if includes:
        document["include"] = [include for include in includes]
    if document_services:
        document["services"] = document_services
    if routes:
        # Render one site with one handle block per route in declaration order.
        lines = [f":{EDGE_LISTEN_PORT} {{"]
        for route in routes:
            matcher = "" if route.path == "/*" else f" {route.path}"
            if route.raw is not None:
                block = ["\t\t" + line for line in route.raw.splitlines()]
            elif route is auth_route:
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

    if routes or ports:
        # Validate every route upstream and port owner against compose's own resolution.
        resolved = set(_compose_config(config.root, compose_files, "--services", env_file=env_file))
        for route in routes:
            upstream = route.upstream.partition(":")[0]
            if upstream not in resolved:
                raise ValueError(
                    f"route {route.path} upstream {upstream} resolves to no service in the selected assembly"
                )
        for port in ports:
            if port.service not in resolved:
                raise ValueError(f"ports for service {port.service!r} resolve to no service in the selected assembly")
    if profiles:
        # Refuse profile names the compose files do not define.
        defined = _compose_config(config.root, compose_files, "--profiles", env_file=env_file)
        unknown = [profile for profile in profiles if profile not in defined]
        if unknown:
            raise ValueError(
                f"COMPOSE_PROFILES selects unknown profile(s) {unknown}; "
                f"profiles defined by the compose files: {defined}"
            )

    return Invocation(
        compose_files=compose_files,
        env_file=env_file,
        profiles=profiles,
        overrides=overrides,
    )


def require_env_file(root: Path) -> None:
    if not (root / ENV_FILE).exists():
        raise ValueError(".env is required")


def namespaced_route(entry: str, route: RouteConfig) -> RouteConfig:
    return route.model_copy(update={"upstream": f"{entry}-{route.upstream}"})


def _compose_config(root: Path, compose_files: list[Path], *arguments: str, env_file: Path | None) -> list[str]:
    return [
        line.strip()
        for line in bash_output(
            compose_command(root, compose_files, "config", *arguments, env_file=env_file)
        ).splitlines()
        if line.strip()
    ]


def compose_command(
    root: Path,
    compose_files: list[Path],
    *arguments: str,
    env_file: Path | None = None,
) -> str:
    quoted_arguments = [shlex.quote(argument) for argument in arguments]
    files_arguments = " ".join(f"-f {shlex.quote(str(compose_file))}" for compose_file in compose_files)
    env_argument = f" --env-file {shlex.quote(str(env_file))}" if env_file is not None else ""
    prefix = f"docker compose {files_arguments} --project-directory {shlex.quote(str(root))} --env-file .env{env_argument}"
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


def write_assembly_env_file(
    config: Config,
    overrides: list[BundleOverride],
    platform: str | None,
    stack: ExitStack,
) -> Path | None:
    lock_path = config.root / LOCK_PATH
    sources: list[tuple[Path, dict[str, str]]] = []
    if lock_path.is_file():
        sources.append((lock_path, compose_env_entries(parse_config_lock(lock_path), config, platform)))
    # A path-form override's bundle output interpolates its producer's third-party holes; the
    # producer's committed lock fills them, derived from the fixed output layout three parents up.
    for override in overrides:
        if override.path is None:
            continue
        producer_lock = override.path.parents[3] / LOCK_PATH
        if producer_lock.is_file():
            sources.append((producer_lock, producer_env_entries(parse_config_lock(producer_lock), platform)))

    entries: dict[str, str] = {}
    filled_by: dict[str, Path] = {}
    for source_lock, filled in sources:
        for key, value in filled.items():
            previous = filled_by.get(key)
            if previous is not None:
                raise ValueError(
                    f"lock key {key} is filled by two locks in the assembly: "
                    f"{previous.as_posix()} and {source_lock.as_posix()}"
                )
            filled_by[key] = source_lock
            entries[key] = value

    if not entries:
        # A bundles-only lock carries nothing the compose environment consumes.
        return None
    return write_env_file(entries, "devkit-lock-env-", stack)


def write_temp_compose_file(stack: ExitStack, document: Mapping[str, Yaml], prefix: str) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix=prefix, delete=False, encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        path = Path(handle.name)
    stack.callback(path.unlink, missing_ok=True)
    return path
