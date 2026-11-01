from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class Selection:
    riding: list[str]
    options: dict[str, str]
    choices: dict[str, str]
    entries: list[tuple[str, ServiceGroup | ServiceEntry]]


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
        environment = merged_environment(root)
        selected = selection(config, environment)
        if selected is None:
            require_compose_stack(config)
        assert selected is not None and config is not None
        self.riding = selected.riding
        self.choices = selected.choices
        self.profiles = selected_profiles(environment)
        self.forwarded = forwarded_pair(environment) if selected.choices.get(AUTH_GROUP_NAME) is not None else None

        # Collect the include list in services declaration order.
        self.includes = []
        local_outputs: list[Path] = []
        for name, entry in selected.entries:
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
        for index, (path, service_names) in enumerate(file_services):
            for other_path, other_service_names in file_services[index + 1 :]:
                shared = sorted(service_names & other_service_names)
                if shared:
                    raise RuntimeError(
                        f"the assembly includes {path.as_posix()} and {other_path.as_posix()}, "
                        f"which both define service(s) {shared}; select one"
                    )
        self.output_services = {service_name for _path, names in file_services for service_name in names}

        # Gather the riding routes, ports, and the selected auth route.
        self.routes = [route for _, entry in selected.entries for route in entry.routes]
        self.ports = [port for _, entry in selected.entries for port in entry.ports]
        self.auth_route = None
        auth = config.services.get(AUTH_GROUP_NAME)
        if isinstance(auth, ServiceGroup):
            option_name = selected.choices.get(AUTH_GROUP_NAME)
            if option_name is not None:
                self.auth_route = auth.options[option_name].routes[0]


def require_compose_stack(config: DockerDevkitConfig | None) -> None:
    if config is None or not config.services:
        raise RuntimeError(
            "no docker-devkit.yaml (or no services: in it); "
            "a builds-only, mirror-only, or k8s-only repo has no compose stack"
        )


def merged_environment(root: Path) -> dict[str, str]:
    require_env_file(root)
    environment = parse_env_file(root / ENV_FILE)
    environment.update(environ)
    return environment


def require_env_file(root: Path) -> None:
    if not (root / ENV_FILE).exists():
        raise RuntimeError("No .env file found; create one first (e.g., copy .env.example)")


def selection(config: DockerDevkitConfig | None, environment: dict[str, str]) -> Selection | None:
    names = _split_names(environment.get(BUNDLES_VARIABLE, ""))
    if config is None or not config.services:
        if names:
            raise RuntimeError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
        return None
    # BUNDLES= names options only: at most one per group, zero legal when riders carry
    # the assembly.
    options = {
        option_name: name
        for name, entry in config.services.items()
        if isinstance(entry, ServiceGroup)
        for option_name in entry.options
    }
    riding = [name for name, entry in config.services.items() if not isinstance(entry, ServiceGroup)]
    if not names and not riding and options:
        raise RuntimeError(
            f"{BUNDLES_VARIABLE}= selects nothing and no bare entry rides; this repo's options are {sorted(options)}"
        )
    unknown = [name for name in names if name not in options]
    if unknown:
        riding_note = f"; riding entries, always on: {riding}" if riding else ""
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

    # The declaration-order flatten every assembly view reads: each group yields itself
    # (its riding routes and ports) plus its selected option, each bare entry yields itself.
    entries: list[tuple[str, ServiceGroup | ServiceEntry]] = []
    for name, entry in config.services.items():
        if isinstance(entry, ServiceGroup):
            entries.append((name, entry))
            option_name = choices.get(name)
            if option_name is not None:
                entries.append((option_name, entry.options[option_name]))
        else:
            entries.append((name, entry))
    return Selection(riding=riding, options=options, choices=choices, entries=entries)


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


def selected_profiles(environment: dict[str, str]) -> list[str]:
    return _split_names(environment.get(PROFILES_VARIABLE, ""))


def assert_known_profiles(profiles: list[str], defined: list[str]) -> None:
    unknown = [profile for profile in profiles if profile not in defined]
    if unknown:
        raise RuntimeError(
            f"COMPOSE_PROFILES selects unknown profile(s) {unknown}; profiles defined by the compose files: {defined}"
        )


def _split_names(value: str) -> list[str]:
    names: list[str] = []
    for entry in value.split(","):
        name = entry.strip()
        if name and name not in names:
            names.append(name)
    return names


def _service_names(path: Path) -> set[str]:
    # Extract names only; authored files never promised the bind-law lens.
    document: dict[str, object] = _mapping_or_empty(yaml.safe_load(path.read_text(encoding="utf-8")))
    return set(_mapping_or_empty(document.get("services")))


def _mapping_or_empty(value: object) -> dict[str, object]:
    return value if _is_mapping(value) else {}


def _is_mapping(value: object) -> TypeIs[dict[str, object]]:
    return isinstance(value, dict)
