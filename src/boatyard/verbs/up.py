from __future__ import annotations

import os
from contextlib import ExitStack
from itertools import groupby
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

import typer
import yaml
from bashrun.bash import bash_handoff

from boatyard.bake import run_build
from boatyard.compose import compose_command, lock_env_unwind, require_env_file
from boatyard.config import (
    CADDY_IMAGE,
    Config,
    EDGE_SERVICE_NAME,
    OCI_REFERENCE_PREFIX,
    ResolvedService,
    Yaml,
    bundle_lock_key,
    compose_bundle_output_path,
    double_dollars,
    is_mapping,
    load_yaml_file,
    lock_key_prefix,
    qualified_option_name,
    refuse_unresolved_references,
    require_compose_stack,
    required_compose_holes,
)
from boatyard.config_lock import LOCK_PATH, parse_config_lock, read_config_lock
from boatyard.environment import BUNDLES_VARIABLE, BUNDLE_OVERRIDE_PREFIX, Environment
from boatyard.lock import compose_env_entries, producer_env_entries, render_env_entries

EDGE_LISTEN_PORT = 8443
EDGE_CONFIG_NAME = "devkit-edge-caddyfile"

app = typer.Typer()


@app.command()
def up(
    attached: Annotated[bool, typer.Option("--attached", "-a", help="Run in foreground (not detached)")] = False,
    quiet_pull: Annotated[
        bool,
        typer.Option("--quiet-pull", "-q", help="Suppress per-layer pull progress (still shows pull/push totals)."),
    ] = False,
    build: Annotated[
        bool,
        typer.Option("--build", help="Build all images locally before bringing the stack up; skips pulling"),
    ] = False,
) -> None:
    root = Path.cwd()
    config = Config.load(root)

    require_env_file(root)
    environment = Environment.load(root, config)
    if config is None or not config.services:
        config = require_compose_stack(config)

    # Refuse --build without build declarations
    if build and not (config.repository or config.base_images):
        raise typer.BadParameter("--build requires build declarations (repository or base_images) in boatyard.yml")

    # Refuse an empty selection in an options-only repo
    if not environment.bundles and not config.bare_entries and config.selectable_options:
        raise ValueError(
            f"{BUNDLES_VARIABLE}= selects nothing and no bare entry exists; "
            f"the options are {sorted(config.selectable_options)}"
        )

    # Fold the selection into at most one option per group
    choices: dict[str, str] = {}
    for name in environment.bundles:
        group, option_name = config.selectable_options[name]
        if group in choices:
            raise ValueError(
                f"{BUNDLES_VARIABLE}= selects {qualified_option_name(group, choices[group])} and {name}; "
                f"at most one option per group ({group})"
            )
        choices[group] = option_name

    # Resolve the chosen options into leaf services
    services = config.resolve_services(choices)

    # Collect each entry's include and env entries in declaration order
    lock = read_config_lock(config.root)
    consumer_entries = compose_env_entries(lock, config, environment.platform)
    entries: dict[str, str] = dict(consumer_entries)
    includes: list[Yaml] = []
    requirements: dict[str, list[str]] = {}

    for service in services:
        name = service.name

        # Include a local entry through its generated output
        if service.entry.ref is None:
            output = compose_bundle_output_path(config.root, name)
            if not output.is_file():
                raise ValueError(
                    f"selected entry {name!r} has no bundle output {output.as_posix()}; generate-score writes it"
                )
            requirements[name] = _scan_local_bundle(name, service, output, refuse_edge=False)
            includes.append(output.resolve().as_posix())
            entries.update({lock_key_prefix(name) + key: value for key, value in consumer_entries.items()})
            continue

        # Include an overridden ref through its local path
        if name in environment.path_overrides:
            candidate = environment.path_overrides[name]
            path = (candidate if candidate.is_absolute() else config.root / candidate).resolve()
            if not path.is_file():
                raise ValueError(
                    f"{BUNDLE_OVERRIDE_PREFIX + name.upper().replace('-', '_')}: {path.as_posix()} does not exist"
                )
            requirements[name] = _scan_local_bundle(name, service, path, refuse_edge=True)
            includes.append(path.as_posix())
            # Fill the override's holes from the producer lock three parents up
            producer_lock = path.parents[3] / LOCK_PATH
            if producer_lock.is_file():
                producer_entries = producer_env_entries(parse_config_lock(producer_lock), environment.platform)
                entries.update({lock_key_prefix(name) + key: value for key, value in producer_entries.items()})
            continue

        # Include a remote ref as its lock-pinned digest
        key = bundle_lock_key(name)
        pinned = lock.require(key, f"bundle {name!r}", hint="run boatyard lock")
        requirements[name] = lock.bundles[key].requires
        includes.append(f"{OCI_REFERENCE_PREFIX}{pinned}")

    # Refuse bundles whose requirements no environment source fills
    filled = {variable for variable, value in {**os.environ, **environment.variables, **entries}.items() if value}
    _refuse_unfilled_requirements(requirements, filled)

    # Derive the forwarded environment from PUBLIC_URL under the auth option
    auth_service = next((service for service in services if service.auth), None)
    forwarded = None
    if auth_service is not None:
        if environment.public_url is None:
            raise ValueError("an auth option is selected; PUBLIC_URL is required")
        forwarded = environment.public_url.forwarded

    # Namespace routes and ports under their declaring entries
    routes = [
        route.model_copy(update={"upstream": f"{service.name}-{route.upstream}"})
        for service in services
        for route in service.routes
    ]
    ports = [
        port.model_copy(update={"service": f"{service.name}-{port.service}"})
        for service in services
        for port in service.ports
    ]

    # Render the edge and its Caddyfile when a route rides
    document_services: dict[str, Yaml] = {}
    caddyfile: str | None = None
    if routes:
        edge: dict[str, Yaml] = {
            "image": f"${{{CADDY_IMAGE}:?err}}",
            "configs": [{"source": EDGE_CONFIG_NAME, "target": "/etc/caddy/Caddyfile"}],
            "ports": [{"target": str(EDGE_LISTEN_PORT), "published": config.gateway, "protocol": "tcp"}],
            # Attach the edge to every selected bundle's network
            "networks": dict.fromkeys(f"{service.name}-net" for service in services),
        }
        if forwarded is not None:
            edge["environment"] = forwarded
        document_services[EDGE_SERVICE_NAME] = edge

        # Render one handle block per route in declaration order
        lines = [f":{EDGE_LISTEN_PORT} {{"]
        for route in routes:
            matcher = "" if route.path == "/*" else f" {route.path}"
            if route.raw is not None:
                block = ["\t\t" + line for line in route.raw.splitlines()]
            elif auth_service is not None and route.path == auth_service.routes[0].path:
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
        caddyfile = double_dollars("\n".join(lines) + "\n")

    # Group the namespaced ports into per-service stanzas
    document_services.update({
        service: {"ports": [port.model_dump(exclude={"service"}, exclude_none=True) for port in service_ports]}
        for service, service_ports in groupby(
            sorted(ports, key=lambda port: port.service), key=lambda port: port.service
        )
    })
    # Assemble the root document
    document: dict[str, Yaml] = {"name": config.project_name}
    if includes:
        document["include"] = includes
    if document_services:
        document["services"] = document_services
    if caddyfile is not None:
        document["configs"] = {EDGE_CONFIG_NAME: {"content": caddyfile}}

    # Materialize the per-run files and hand off
    with ExitStack() as stack:
        # Write the per-run lock env file
        env_file: Path | None = None
        if entries:
            with NamedTemporaryFile(
                "w", suffix=".env", prefix="devkit-lock-env-", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(render_env_entries(entries))
                env_file = Path(handle.name)
            stack.enter_context(lock_env_unwind(env_file))

        with NamedTemporaryFile(
            "w", suffix=".yaml", prefix="devkit-assembly-", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        assembly = Path(handle.name)
        stack.callback(assembly.unlink, missing_ok=True)
        compose_files = [assembly]

        # Build local images
        if build:
            run_build(config=config)

        # Hand off to docker compose
        up_command = compose_command(
            root,
            compose_files,
            "up",
            env_file=env_file,
        )
        if not build:
            up_command += " --pull missing"
            if quiet_pull:
                up_command += " --quiet-pull"
        if not attached:
            up_command += " -d"

        bash_handoff(up_command)


def _scan_local_bundle(name: str, service: ResolvedService, path: Path, *, refuse_edge: bool) -> list[str]:
    loaded = load_yaml_file(path)
    loaded_services = loaded.get("services") if is_mapping(loaded) else None
    if refuse_edge and is_mapping(loaded_services) and EDGE_SERVICE_NAME in loaded_services:
        raise ValueError(f"{path.as_posix()}: the bundle override defines the reserved service {EDGE_SERVICE_NAME!r}")
    refuse_unresolved_references(
        name,
        service.routes,
        service.ports,
        set(loaded_services) if is_mapping(loaded_services) else set(),
        prefix=f"{name}-",
    )
    return sorted(required_compose_holes(path.read_text(encoding="utf-8")))


def _refuse_unfilled_requirements(requirements: dict[str, list[str]], filled: set[str]) -> None:
    missing = {name: sorted(set(required) - filled) for name, required in requirements.items() if set(required) - filled}
    if missing:
        name, absent = min(missing.items())
        raise ValueError(
            f"bundle {name!r} requires {absent}, which no environment source sets (shell, .env, or the lock env file)"
        )
