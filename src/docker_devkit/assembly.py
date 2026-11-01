from __future__ import annotations

from itertools import combinations
from os import environ
from pathlib import Path
from typing import TypeIs

import yaml

from .config import (
    AUTH_GROUP_NAME,
    DockerDevkitConfig,
    PortDeclaration,
    RouteConfig,
    ServiceEntry,
    ServiceGroup,
    bundle_output,
)
from .host import parse_env_file

ENV_FILE = Path(".env")
BUNDLES_VARIABLE = "BUNDLES"
PROFILES_VARIABLE = "COMPOSE_PROFILES"


class Assembly:
    # The selected assembly as one record: every environment read and selection
    # derivation runs exactly once in the constructor, and every refusal those
    # derivations own fires there. Inputs (config, environment) stay at call sites.
    riding: list[str]
    choices: dict[str, str]
    profiles: list[str]
    forwarded: dict[str, str] | None
    includes: list[str]
    output_services: set[str]
    routes: list[RouteConfig]
    ports: list[PortDeclaration]
    auth_route: RouteConfig | None

    def __init__(self, root: Path, config: DockerDevkitConfig | None) -> None:
        require_env_file(root)
        environment = parse_env_file(root / ENV_FILE)
        environment.update(environ)

        names = _split_names(environment.get(BUNDLES_VARIABLE, ""))
        if config is None or not config.services:
            if names:
                raise RuntimeError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
            require_compose_stack(config)
        assert config is not None

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

    def assert_resolves(self, resolved: set[str]) -> None:
        # Compose's own resolution is the authority on what the selected assembly
        # defines: every route upstream (the auth option's route rides the list by
        # construction, its option being a selected entry) and every port's owning
        # service must appear in it.
        for route in self.routes:
            upstream = route.upstream.partition(":")[0]
            if upstream not in resolved:
                raise RuntimeError(
                    f"route {route.path} upstream {upstream} resolves to no service in the selected assembly"
                )
        for port in self.ports:
            if port.service not in resolved:
                raise RuntimeError(f"ports for service {port.service!r} resolve to no service in the selected assembly")


def require_env_file(root: Path) -> None:
    if not (root / ENV_FILE).exists():
        raise RuntimeError("No .env file found; create one first (e.g., copy .env.example)")


def require_compose_stack(config: DockerDevkitConfig | None) -> None:
    if config is None or not config.services:
        raise RuntimeError(
            "no docker-devkit.yaml (or no services: in it); "
            "a builds-only, mirror-only, or k8s-only repo has no compose stack"
        )


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
