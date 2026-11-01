from __future__ import annotations

from collections.abc import Mapping
from itertools import combinations, groupby
from os import environ
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypeIs

import yaml
from bashrun.bash import bash_output

from .config import (
    AUTH_GROUP_NAME,
    CADDY_IMAGE,
    DEV_TOGGLES_ANNOTATION,
    DOOR_SERVICE_NAME,
    DockerDevkitConfig,
    LOCK_PATH,
    PortDeclaration,
    RouteConfig,
    ServiceEntry,
    ServiceGroup,
    bundle_output,
    compose_bundles,
    parse_dev_toggles,
)
from .host import parse_env_file

ENV_FILE = Path(".env")
BUNDLES_VARIABLE = "BUNDLES"
PROFILES_VARIABLE = "COMPOSE_PROFILES"

DEV_MOUNT_TARGET = "/app"

DOOR_LISTEN_PORT = 8443
DOOR_CONFIG_NAME = "devkit-door-caddyfile"
DOOR_CONFIG_TARGET = "/etc/caddy/Caddyfile"
CADDY_IMAGE_HOLE = f"${{{CADDY_IMAGE}:?err}}"


class Assembly:
    # The selected assembly as one record, rendered and materialized: every
    # environment read, selection derivation, and document render runs exactly
    # once in the constructor, and every refusal those steps own fires there.
    # The record retains its inputs (root, config) for the render methods.
    root: Path
    config: DockerDevkitConfig
    riding: list[str]
    choices: dict[str, str]
    profiles: list[str]
    forwarded: dict[str, str] | None
    includes: list[str]
    output_services: set[str]
    routes: list[RouteConfig]
    ports: list[PortDeclaration]
    auth_route: RouteConfig | None
    compose_files: list[Path]

    def __init__(self, root: Path, config: DockerDevkitConfig | None, dev: bool = False) -> None:
        self.root = root
        require_env_file(root)
        environment = parse_env_file(root / ENV_FILE)
        environment.update(environ)

        names = _split_names(environment.get(BUNDLES_VARIABLE, ""))
        if config is None or not config.services:
            if names:
                raise RuntimeError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
            require_compose_stack(config)
        assert config is not None
        self.config = config

        # BUNDLES= names options only: at most one per group, zero legal when riders
        # carry the assembly.
        options = {
            option_name: name
            for name, entry in config.services.items()
            if isinstance(entry, ServiceGroup)
            for option_name in entry.options
        }
        self.riding = [name for name, entry in config.services.items() if not isinstance(entry, ServiceGroup)]
        if not names and not self.riding and options:
            raise RuntimeError(
                f"{BUNDLES_VARIABLE}= selects nothing and no bare entry rides; this repo's options are {sorted(options)}"
            )
        unknown = [name for name in names if name not in options]
        if unknown:
            riding_note = f"; riding entries, always on: {self.riding}" if self.riding else ""
            raise RuntimeError(
                f"{BUNDLES_VARIABLE}= names {unknown}; the selectable options are {sorted(options)}{riding_note}"
            )
        self.choices = {}
        for name in names:
            group = options[name]
            if group in self.choices:
                raise RuntimeError(
                    f"{BUNDLES_VARIABLE}= selects {self.choices[group]} and {name}; "
                    f"at most one option per group ({group})"
                )
            self.choices[group] = name

        self.profiles = _split_names(environment.get(PROFILES_VARIABLE, ""))
        self.forwarded = forwarded_pair(environment) if self.choices.get(AUTH_GROUP_NAME) is not None else None

        # The declaration-order flatten every assembly view reads: each group yields
        # itself (its riding routes and ports) plus its selected option, each bare
        # entry yields itself.
        entries: list[tuple[str, ServiceGroup | ServiceEntry]] = []
        for name, entry in config.services.items():
            if isinstance(entry, ServiceGroup):
                entries.append((name, entry))
                option_name = self.choices.get(name)
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
            output = root / bundle_output(name)
            if not output.is_file():
                raise RuntimeError(
                    f"selected entry {name!r} has no bundle output {output.as_posix()}; generate-score writes it"
                )
            self.includes.append(output.resolve().as_posix())
            local_outputs.append(output)

        # Refuse selected outputs that define the same service name.
        file_services = [(path, _service_names(path)) for path in local_outputs]
        for (path, service_names), (other_path, other_service_names) in combinations(file_services, 2):
            shared = sorted(service_names & other_service_names)
            if shared:
                raise RuntimeError(
                    f"the assembly includes {path.as_posix()} and {other_path.as_posix()}, "
                    f"which both define service(s) {shared}; select one"
                )
        self.output_services = {name for _path, names in file_services for name in names}

        # Gather the riding routes, ports, and the selected auth route.
        self.routes = [route for _, entry in entries for route in entry.routes]
        self.ports = [port for _, entry in entries for port in entry.ports]
        self.auth_route = None
        auth = config.services.get(AUTH_GROUP_NAME)
        if isinstance(auth, ServiceGroup):
            option_name = self.choices.get(AUTH_GROUP_NAME)
            if option_name is not None:
                self.auth_route = auth.options[option_name].routes[0]

        # Materialize and validate in one step: the per-run compose files (root
        # first, dev overlay last — the -f order, so the overlay overrides) and
        # compose's own resolution over them; a failure unlinks what was written.
        self.compose_files = []
        try:
            self._materialize_and_validate(dev)
        except BaseException:
            self.cleanup()
            raise

    def _materialize_and_validate(self, dev: bool) -> None:
        self.compose_files.append(write_temp_compose_file(self._render_root_document(), "devkit-assembly-"))
        if dev:
            self.compose_files.append(write_temp_compose_file(self._render_dev_overlay(), "devkit-dev-overlay-"))
        if self.routes or self.ports:
            # Validate every wired upstream and port owner against compose's own
            # resolution (the auth option's route rides the routes list by
            # construction, its option being a selected entry).
            resolved = set(_compose_config(self.root, self.compose_files, "--services"))
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
            assert_known_profiles(self.profiles, _compose_config(self.root, self.compose_files, "--profiles"))

    def cleanup(self) -> None:
        for compose_file in self.compose_files:
            compose_file.unlink(missing_ok=True)

    def _render_root_document(self) -> dict[str, object]:
        # The per-run root compose file: name, includes, the door, parent port stanzas, Caddyfile.
        assert self.config.project_name is not None
        services: dict[str, object] = {}
        if self.routes:
            assert self.config.gateway is not None
            services[DOOR_SERVICE_NAME] = {
                "image": CADDY_IMAGE_HOLE,
                "configs": [{"source": DOOR_CONFIG_NAME, "target": DOOR_CONFIG_TARGET}],
                "ports": [{"target": str(DOOR_LISTEN_PORT), "published": self.config.gateway, "protocol": "tcp"}],
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
            document["configs"] = {DOOR_CONFIG_NAME: {"content": self._render_caddyfile()}}
        return document

    def _render_caddyfile(self) -> str:
        # One site on the internal port; each route one handle block, in declaration order
        # (caddy sorts them longest-path-first, so the order cannot shadow).
        lines = [f":{DOOR_LISTEN_PORT} {{"]
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
        # Every $ doubles: compose interpolation would otherwise eat caddy's {$ENV} placeholders.
        return ("\n".join(lines) + "\n").replace("$", "$$")

    def _render_dev_overlay(self) -> dict[str, object]:
        # The --dev overlay: dev-stage images with the repo bind, plus dev-toggle env holes.
        declared = self.output_services
        services: dict[str, dict[str, object]] = {
            name: {
                "image": f"{self.config.repository}/{name}:dev",
                "volumes": [
                    {
                        "type": "bind",
                        "source": self.root.resolve().as_posix(),
                        "target": DEV_MOUNT_TARGET,
                        "read_only": True,
                    }
                ],
            }
            for name, service in self.config.service_builds.items()
            if service.dev_stage and name in declared
        }
        for bundle in compose_bundles(self.config):
            for workload in bundle.workloads:
                workload_document: dict[str, object] = mapping_or_empty(
                    yaml.safe_load((self.root / self.config.score_dir / workload).read_text(encoding="utf-8"))
                )
                metadata: dict[str, object] = mapping_or_empty(workload_document.get("metadata"))
                annotations: dict[str, object] = mapping_or_empty(metadata.get("annotations"))
                raw: object | None = annotations.get(DEV_TOGGLES_ANNOTATION)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                service_name = metadata.get("name")
                if not isinstance(service_name, str) or not service_name or service_name not in declared:
                    continue
                toggles = parse_dev_toggles(service_name, raw)
                service = services.setdefault(service_name, {})
                service["environment"] = {key: f"${{{key}:-{value}}}" for key, value in toggles.items()}
        if not services:
            raise RuntimeError(
                "--dev found no dev shape in the assembly: a service Dockerfile declaring a "
                f"dev stage or a workload carrying {DEV_TOGGLES_ANNOTATION}"
            )
        return {"services": services}


def require_env_file(root: Path) -> None:
    if not (root / ENV_FILE).exists():
        raise RuntimeError("No .env file found; create one first (e.g., copy .env.example)")


def require_compose_stack(config: DockerDevkitConfig | None) -> None:
    if config is None or not config.services:
        raise RuntimeError(
            "no docker-devkit.yaml (or no services: in it); "
            "a builds-only, mirror-only, or k8s-only repo has no compose stack"
        )


def compose_command(root: Path, compose_files: list[Path], *arguments: str) -> str:
    files_arguments = " ".join(f"-f {compose_file}" for compose_file in compose_files)
    lock_file = root / LOCK_PATH
    lock_argument = f" --env-file {lock_file}" if lock_file.exists() else ""
    prefix = f"docker compose {files_arguments} --project-directory {root} --env-file .env{lock_argument}"
    return " ".join([prefix, *arguments])


def write_temp_compose_file(document: Mapping[str, object], prefix: str) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix=prefix, delete=False, encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        return Path(handle.name)


def _compose_config(root: Path, compose_files: list[Path], *arguments: str) -> list[str]:
    return [
        line.strip()
        for line in bash_output(compose_command(root, compose_files, "config", *arguments)).splitlines()
        if line.strip()
    ]


def assert_known_profiles(profiles: list[str], defined: list[str]) -> None:
    unknown = [profile for profile in profiles if profile not in defined]
    if unknown:
        raise RuntimeError(
            f"COMPOSE_PROFILES selects unknown profile(s) {unknown}; profiles defined by the compose files: {defined}"
        )


def forwarded_pair(environment: dict[str, str]) -> dict[str, str]:
    public_url = environment.get("PUBLIC_URL", "").strip()
    if not public_url:
        raise RuntimeError(
            "an auth option is selected, so the door derives the forwarded scheme and port from PUBLIC_URL; "
            "PUBLIC_URL is missing from the environment"
        )
    scheme, _, rest = public_url.partition("://")
    hostport = rest.partition("/")[0]
    if ":" in hostport:
        forwarded_port = hostport.rpartition(":")[2]
    elif scheme == "https":
        forwarded_port = "443"
    else:
        forwarded_port = "80"
    return {"FORWARDED_PROTO": scheme, "FORWARDED_PORT": forwarded_port}


def _split_names(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def _service_names(path: Path) -> set[str]:
    # Extract names only; authored files never promised the bind-law lens.
    document: dict[str, object] = mapping_or_empty(yaml.safe_load(path.read_text(encoding="utf-8")))
    return set(mapping_or_empty(document.get("services")))


def mapping_or_empty(value: object) -> dict[str, object]:
    return value if _is_mapping(value) else {}


def _is_mapping(value: object) -> TypeIs[dict[str, object]]:
    return isinstance(value, dict)
