from __future__ import annotations

import shlex
from collections.abc import Mapping
from contextlib import ExitStack
from itertools import combinations, groupby
from os import environ
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import Self

import yaml
from bashrun.bash import bash_output

from docker_devkit.config import (
    AUTH_GROUP_NAME,
    CADDY_IMAGE,
    DEV_TOGGLES_ANNOTATION,
    DockerDevkitConfig,
    EDGE_SERVICE_NAME,
    PortDeclaration,
    RouteConfig,
    ServiceEntry,
    ServiceGroup,
    is_mapping,
    parse_dev_toggles,
    require_compose_stack,
)
from docker_devkit.config_lock import LOCK_PATH, parse_image_lock
from docker_devkit.documents import WorkloadDocument
from docker_devkit.lock import compose_env_entries, render_env_entries

ENV_FILE = Path(".env")
BUNDLES_VARIABLE = "BUNDLES"

EDGE_LISTEN_PORT = 8443
EDGE_CONFIG_NAME = "devkit-edge-caddyfile"


class Assembly:
    root: Path
    config: DockerDevkitConfig
    profiles: list[str]
    forwarded: dict[str, str] | None
    includes: list[str]
    output_services: set[str]
    routes: list[RouteConfig]
    ports: list[PortDeclaration]
    auth_route: RouteConfig | None
    compose_files: list[Path]
    lock_env_file: Path | None

    def __init__(self, root: Path, config: DockerDevkitConfig | None, stack: ExitStack, dev: bool = False) -> None:
        self.root = root
        require_env_file(root)
        environment = merged_environment(root)

        names = _split_names(environment.get(BUNDLES_VARIABLE, ""))
        if config is None or not config.services:
            if names:
                raise RuntimeError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
            config = require_compose_stack(config)
        self.config = config

        options = {
            option_name: name
            for name, entry in config.services.items()
            if isinstance(entry, ServiceGroup)
            for option_name in entry.options
        }
        riding = [name for name, entry in config.services.items() if not isinstance(entry, ServiceGroup)]
        if not names and not riding and options:
            raise RuntimeError(
                f"{BUNDLES_VARIABLE}= selects nothing and no bare entry exists; the options are {sorted(options)}"
            )
        unknown = [name for name in names if name not in options]
        if unknown:
            riding_note = f"; bare entries, always included: {riding}" if riding else ""
            raise RuntimeError(
                f"{BUNDLES_VARIABLE}= names {unknown}; the selectable options are {sorted(options)}{riding_note}"
            )
        choices: dict[str, str] = {}
        for name in names:
            group = options[name]
            if group in choices:
                raise RuntimeError(
                    f"{BUNDLES_VARIABLE}= selects {choices[group]} and {name}; at most one option per group ({group})"
                )
            choices[group] = name

        auth_option = choices.get(AUTH_GROUP_NAME)

        self.profiles = _split_names(environment.get("COMPOSE_PROFILES", ""))
        self.forwarded = None
        if auth_option is not None:
            public_url = environment.get("PUBLIC_URL", "").strip()
            if not public_url:
                raise RuntimeError("an auth option is selected; PUBLIC_URL is required")
            scheme, _, rest = public_url.partition("://")
            hostport = rest.partition("/")[0]
            if ":" in hostport:
                forwarded_port = hostport.rpartition(":")[2]
            elif scheme == "https":
                forwarded_port = "443"
            else:
                forwarded_port = "80"
            self.forwarded = {"FORWARDED_PROTO": scheme, "FORWARDED_PORT": forwarded_port}

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
        self.includes = []
        local_outputs: list[Path] = []
        for name, entry in entries:
            if isinstance(entry, ServiceGroup):
                continue
            if entry.ref is not None:
                self.includes.append(entry.ref)
                continue
            output = root / "stack/generated/compose" / f"compose.{name}.yaml"
            if not output.is_file():
                raise RuntimeError(
                    f"selected entry {name!r} has no bundle output {output.as_posix()}; generate-score writes it"
                )
            self.includes.append(output.resolve().as_posix())
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
                raise RuntimeError(f"{path.as_posix()} and {other_path.as_posix()} both define service(s) {shared}")
        self.output_services = {name for _path, names in file_services for name in names}

        # Gather the routes, ports, and the selected auth route.
        self.routes = [route for _, entry in entries for route in entry.routes]
        self.ports = [port for _, entry in entries for port in entry.ports]
        self.auth_route = None
        auth = config.services.get(AUTH_GROUP_NAME)
        if isinstance(auth, ServiceGroup) and auth_option is not None:
            self.auth_route = auth.options[auth_option].routes[0]

        # Materialize the per-run files.
        self.lock_env_file = write_lock_env_file(root, self.config, environment, stack)
        self.compose_files = []

        # Render the per-run root compose file.
        assert self.config.project_name is not None
        services: dict[str, object] = {}
        if self.routes:
            if self.config.gateway is None:
                raise RuntimeError("routes are declared; gateway is required")
            services[EDGE_SERVICE_NAME] = {
                "image": f"${{{CADDY_IMAGE}:?err}}",
                "configs": [{"source": EDGE_CONFIG_NAME, "target": "/etc/caddy/Caddyfile"}],
                "ports": [{"target": str(EDGE_LISTEN_PORT), "published": self.config.gateway, "protocol": "tcp"}],
                **({"environment": self.forwarded} if self.forwarded is not None else {}),
            }
        ordered_ports = sorted(self.ports, key=lambda port: port.service)
        services.update({
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
        })
        document: dict[str, object] = {"name": self.config.project_name}
        if self.includes:
            document["include"] = self.includes
        if services:
            document["services"] = services
        if self.routes:
            # Render one site with one handle block per route in declaration order.
            lines = [f":{EDGE_LISTEN_PORT} {{"]
            for route in self.routes:
                matcher = "" if route.path == "/*" else f" {route.path}"
                if route.raw is not None:
                    block = ["\t\t" + line for line in route.raw.splitlines()]
                elif route is self.auth_route:
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
            # Double every $.
            document["configs"] = {EDGE_CONFIG_NAME: {"content": ("\n".join(lines) + "\n").replace("$", "$$")}}
        self.compose_files.append(write_temp_compose_file(stack, document, "devkit-assembly-"))

        if dev:
            # Render the dev overlay.
            declared = self.output_services
            overlay_services: dict[str, dict[str, object]] = {
                name: {
                    "image": f"{self.config.repository}/{name}:dev",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": self.root.resolve().as_posix(),
                            "target": "/app",
                            "read_only": True,
                        }
                    ],
                }
                for name, service in self.config.service_builds.items()
                if service.dev_stage and name in declared
            }
            for bundle in self.config.compose_bundles():
                for workload in bundle.workloads:
                    workload_document = WorkloadDocument.model_validate(
                        yaml.safe_load((self.config.score_dir_path() / workload).read_text(encoding="utf-8"))
                    )
                    raw, service_name = workload_document.dev_toggles
                    # Skip absent annotations and outside-selection names; refuse malformed shapes.
                    if not isinstance(raw, str | None):
                        raise TypeError(f"workload {workload} carries a malformed {DEV_TOGGLES_ANNOTATION} annotation")
                    if raw is None or not raw.strip():
                        continue
                    if not isinstance(service_name, str):
                        raise TypeError(
                            f"workload {workload} carries {DEV_TOGGLES_ANNOTATION} but no string metadata name"
                        )
                    if service_name not in declared:
                        continue
                    toggles = parse_dev_toggles(service_name, raw)
                    service = overlay_services.setdefault(service_name, {})
                    service["environment"] = {key: f"${{{key}:-{value}}}" for key, value in toggles.items()}
            if not overlay_services:
                raise RuntimeError(
                    f"--dev requires a dev shape: a service Dockerfile declaring a dev stage "
                    f"or a workload carrying {DEV_TOGGLES_ANNOTATION}"
                )
            self.compose_files.append(
                write_temp_compose_file(stack, {"services": overlay_services}, "devkit-dev-overlay-")
            )

        if self.routes or self.ports:
            # Validate every route upstream and port owner against compose's own resolution.
            resolved = set(
                _compose_config(self.root, self.compose_files, "--services", lock_env_file=self.lock_env_file)
            )
            for route in self.routes:
                upstream = route.upstream.partition(":")[0]
                if upstream not in resolved:
                    raise RuntimeError(
                        f"route {route.path} upstream {upstream} resolves to no service in the selected assembly"
                    )
            for port in self.ports:
                if port.service not in resolved:
                    raise RuntimeError(
                        f"ports for service {port.service!r} resolve to no service in the selected assembly"
                    )
        if self.profiles:
            # Refuse profile names the compose files do not define.
            defined = _compose_config(self.root, self.compose_files, "--profiles", lock_env_file=self.lock_env_file)
            unknown = [profile for profile in self.profiles if profile not in defined]
            if unknown:
                raise RuntimeError(
                    f"COMPOSE_PROFILES selects unknown profile(s) {unknown}; "
                    f"profiles defined by the compose files: {defined}"
                )


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


def require_env_file(root: Path) -> None:
    if not (root / ENV_FILE).exists():
        raise RuntimeError(".env is required")


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


def write_lock_env_file(
    root: Path, config: DockerDevkitConfig, environment: Mapping[str, str], stack: ExitStack
) -> Path | None:
    lock_path = root / LOCK_PATH
    if not lock_path.is_file():
        return None
    entries = compose_env_entries(parse_image_lock(lock_path), config, environment)
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


def write_temp_compose_file(stack: ExitStack, document: Mapping[str, object], prefix: str) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix=prefix, delete=False, encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        path = Path(handle.name)
    stack.callback(path.unlink, missing_ok=True)
    return path


def _split_names(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
