from __future__ import annotations

import shlex
from collections.abc import Mapping
from contextlib import ExitStack
from itertools import combinations, groupby
from os import environ
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import NamedTuple, Self

import yaml
from bashrun.bash import bash_output

from boatyard.config import (
    AUTH_GROUP_NAME,
    CADDY_IMAGE,
    Config,
    DEV_TOGGLES_ANNOTATION,
    EDGE_SERVICE_NAME,
    PortDeclaration,
    RouteConfig,
    ServiceEntry,
    ServiceGroup,
    ComposeWorkloadDocument,
    Yaml,
    double_dollars,
    is_mapping,
    parse_dev_toggles,
    validate,
)
from boatyard.config_lock import LOCK_PATH, parse_config_lock
from boatyard.lock import compose_env_entries, render_env_entries

ENV_FILE = Path(".env")
BUNDLES_VARIABLE = "BUNDLES"

EDGE_LISTEN_PORT = 8443
EDGE_CONFIG_NAME = "devkit-edge-caddyfile"


class DevFacts(NamedTuple):
    services: list[str]
    toggles: dict[str, dict[str, str]]


class Selection(NamedTuple):
    includes: list[str]
    routes: list[RouteConfig]
    ports: list[PortDeclaration]
    auth_route: RouteConfig | None
    forwarded: dict[str, str] | None
    profiles: list[str]
    output_services: set[str]
    dev_facts: DevFacts


class Invocation(NamedTuple):
    compose_files: list[Path]
    lock_env_file: Path | None
    profiles: list[str]


class LockEnvUnwind:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self.path is None:
            return None
        if exc_type is not None:
            keep_lock_env_file(self.path)
        else:
            self.path.unlink(missing_ok=True)
        return None


def select(config: Config, environment: Mapping[str, str]) -> Selection:
    names = split_names(environment.get(BUNDLES_VARIABLE, ""))
    root = config.root

    options = {
        option_name: name
        for name, entry in config.services.items()
        if isinstance(entry, ServiceGroup)
        for option_name in entry.options
    }
    riding = [name for name, entry in config.services.items() if not isinstance(entry, ServiceGroup)]
    if not names and not riding and options:
        raise ValueError(
            f"{BUNDLES_VARIABLE}= selects nothing and no bare entry exists; the options are {sorted(options)}"
        )
    unknown = [name for name in names if name not in options]
    if unknown:
        riding_note = f"; bare entries, always included: {riding}" if riding else ""
        raise ValueError(
            f"{BUNDLES_VARIABLE}= names {unknown}; the selectable options are {sorted(options)}{riding_note}"
        )
    choices: dict[str, str] = {}
    for name in names:
        group = options[name]
        if group in choices:
            raise ValueError(
                f"{BUNDLES_VARIABLE}= selects {choices[group]} and {name}; at most one option per group ({group})"
            )
        choices[group] = name

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
                entries.append((option_name, entry.options[option_name]))
        else:
            entries.append((name, entry))

    # Collect the include list in services declaration order.
    includes: list[str] = []
    local_outputs: list[Path] = []
    for name, entry in entries:
        if isinstance(entry, ServiceGroup):
            continue
        if entry.ref is not None:
            includes.append(entry.ref)
            continue
        output = root / "stack/generated/compose" / f"compose.{name}.yaml"
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
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        document_services = loaded.get("services") if is_mapping(loaded) else None
        file_services.append((path, set(document_services) if is_mapping(document_services) else set()))
    for (path, service_names), (other_path, other_service_names) in combinations(file_services, 2):
        shared = sorted(service_names & other_service_names)
        if shared:
            raise ValueError(f"{path.as_posix()} and {other_path.as_posix()} both define service(s) {shared}")
    output_services = {name for _path, names in file_services for name in names}

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
    for bundle in config.compose_bundles():
        for workload in bundle.workloads:
            source_path = config.root / config.score_dir / workload
            try:
                document = yaml.safe_load(source_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as error:
                raise ValueError(f"{workload}: {error}") from error
            annotations = validate(ComposeWorkloadDocument, document, workload).metadata.annotations
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
        output_services=output_services,
        dev_facts=DevFacts(services=dev_services, toggles=dev_toggles),
    )


def render(
    root: Path,
    config: Config,
    environment: Mapping[str, str],
    selection: Selection,
    stack: ExitStack,
    dev: bool = False,
) -> Invocation:
    # Materialize the per-run files.
    lock_env_file = write_lock_env_file(root, config, environment, stack)
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
        for name in selection.dev_facts.services:
            overlay_services[name] = {
                "image": f"{config.repository}/{name}:dev",
                "volumes": [
                    {
                        "type": "bind",
                        "source": root.resolve().as_posix(),
                        "target": "/app",
                        "read_only": True,
                    }
                ],
            }
        for service_name, toggles in selection.dev_facts.toggles.items():
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
        resolved = set(_compose_config(root, compose_files, "--services", lock_env_file=lock_env_file))
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
        defined = _compose_config(root, compose_files, "--profiles", lock_env_file=lock_env_file)
        unknown = [profile for profile in selection.profiles if profile not in defined]
        if unknown:
            raise ValueError(
                f"COMPOSE_PROFILES selects unknown profile(s) {unknown}; "
                f"profiles defined by the compose files: {defined}"
            )

    return Invocation(compose_files=compose_files, lock_env_file=lock_env_file, profiles=selection.profiles)


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
    root: Path, compose_files: list[Path], *arguments: str, lock_env_file: Path | None = None
) -> list[str]:
    return [
        line.strip()
        for line in bash_output(
            compose_command(root, compose_files, "config", *arguments, lock_env_file=lock_env_file)
        ).splitlines()
        if line.strip()
    ]


def compose_command(root: Path, compose_files: list[Path], *arguments: str, lock_env_file: Path | None = None) -> str:
    quoted_arguments = [shlex.quote(argument) for argument in arguments]
    files_arguments = " ".join(f"-f {shlex.quote(str(compose_file))}" for compose_file in compose_files)
    lock_argument = f" --env-file {shlex.quote(str(lock_env_file))}" if lock_env_file is not None else ""
    prefix = (
        f"docker compose {files_arguments} --project-directory {shlex.quote(str(root))} --env-file .env{lock_argument}"
    )
    return " ".join([prefix, *quoted_arguments])


def write_lock_env_file(root: Path, config: Config, environment: Mapping[str, str], stack: ExitStack) -> Path | None:
    lock_path = root / LOCK_PATH
    if not lock_path.is_file():
        return None
    entries = compose_env_entries(parse_config_lock(lock_path), config, environment)
    with NamedTemporaryFile("w", suffix=".env", prefix="devkit-lock-env-", delete=False, encoding="utf-8") as handle:
        handle.write(render_env_entries(entries))
        path = Path(handle.name)
    stack.enter_context(LockEnvUnwind(path))
    return path


def keep_lock_env_file(path: Path | None) -> None:
    if path is None:
        return
    print(f"lock env file kept for inspection: {path}")
    print(path.read_text(encoding="utf-8"), end="")


def write_temp_compose_file(stack: ExitStack, document: Mapping[str, Yaml], prefix: str) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix=prefix, delete=False, encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        path = Path(handle.name)
    stack.callback(path.unlink, missing_ok=True)
    return path


def split_names(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
