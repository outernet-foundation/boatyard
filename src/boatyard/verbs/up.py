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
from boatyard.config.config import (
    CADDY_IMAGE,
    ConfigFile,
    EDGE_SERVICE_NAME,
    OCI_REFERENCE_PREFIX,
    Yaml,
    bundle_lock_key,
    compose_bundle_output_path,
    double_dollars,
    lock_key_prefix,
    require_compose_stack,
    scan_bundle_texts,
)
from boatyard.lock import render_env_entries
from boatyard.config.lock import LockFile
from boatyard.config.values import ValuesFile
from boatyard.paths import DEFAULT_CONFIG_PATH, LOCK_PATH, VALUES_FILE

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
    config = ConfigFile.load(root / DEFAULT_CONFIG_PATH)

    require_env_file(root)
    config_values = ValuesFile.load(root / VALUES_FILE, config)
    require_compose_stack(config)

    # Resolve the chosen options into leaf services
    services = config.resolve_services(config_values.choices)

    # Collect each entry's include and env entries in declaration order
    lock = LockFile.load(config.root / LOCK_PATH, config)
    consumer_entries = lock.manifest_references(config_values.platform)
    entries: dict[str, str] = dict(consumer_entries)
    includes: list[Yaml] = []
    requirements: dict[str, list[str]] = {}

    for service in services:
        name = service.name

        # Include a remote ref as its lock-pinned digest
        if service.entry.ref is not None and name not in config_values.path_overrides:
            key = bundle_lock_key(name)
            pinned = lock.require(key, f"bundle {name!r}")
            requirements[name] = lock.bundles[key].requires
            includes.append(f"{OCI_REFERENCE_PREFIX}{pinned}")
            continue

        # Include a local entry through its generated output
        if service.entry.ref is None:
            output = compose_bundle_output_path(config.root, name)
            if not output.is_file():
                raise ValueError(
                    f"selected entry {name!r} has no bundle output {output.as_posix()}; generate-score writes it"
                )
            text = output.read_text(encoding="utf-8")
            requirements[name] = scan_bundle_texts(name, service.routes, service.ports, [text]).requires
            includes.append(output.resolve().as_posix())
            entries.update({lock_key_prefix(name) + key: value for key, value in consumer_entries.items()})
            continue

        # Include an overridden ref through its local path
        candidate = config_values.path_overrides[name]
        path = (candidate if candidate.is_absolute() else config.root / candidate).resolve()
        if not path.is_file():
            raise ValueError(f"path_overrides.{name}: {path.as_posix()} does not exist")
        text = path.read_text(encoding="utf-8")
        scan = scan_bundle_texts(name, service.routes, service.ports, [text])
        if EDGE_SERVICE_NAME in scan.service_names:
            raise ValueError(f"{path.as_posix()}: the bundle override defines the reserved service {EDGE_SERVICE_NAME!r}")
        requirements[name] = scan.requires
        includes.append(path.as_posix())
        # Fill the override's slots from the producer repo above the override
        producer_root = _producer_root(path)
        producer_config = ConfigFile.load(producer_root / DEFAULT_CONFIG_PATH)
        producer_entries = LockFile.load(producer_root / LOCK_PATH, producer_config).manifest_references(
            config_values.platform
        )
        entries.update({lock_key_prefix(name) + key: value for key, value in producer_entries.items()})

    # Refuse bundles whose requirements no environment source fills; the ambient
    # shell fills nothing, so a shell-set slot name is named as ignored, not counted.
    filled = {variable for variable, value in {**config_values.variables, **entries}.items() if value}
    missing = {
        name: sorted(set(required) - filled) for name, required in requirements.items() if set(required) - filled
    }
    if missing:
        name, absent = min(missing.items())
        ignored = sorted(shell_name for shell_name in absent if os.environ.get(shell_name))
        note = f"; {', '.join(ignored)} set in the shell and ignored" if ignored else ""
        raise ValueError(
            f"bundle {name!r} requires {absent}, which no environment source sets "
            f"(boatyard.values.yaml or the lock env file){note}"
        )

    # Derive the forwarded environment from public_url under the auth option
    auth_service = next((service for service in services if service.auth), None)
    forwarded = None
    if auth_service is not None:
        if config_values.public_url is None:
            raise ValueError("an auth option is selected; public_url is required in boatyard.values.yaml")
        forwarded = config_values.public_url.forwarded

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
        # Write the one per-run dotenv: consumer values + lock entries
        env_file: Path | None = None
        if config_values.variables or entries:
            shared = sorted(set(config_values.variables) & set(entries))
            if shared:
                raise ValueError(
                    f"{VALUES_FILE.name} sets {shared}, which the lock env file also fills; "
                    f"the lock is their only source — remove them from {VALUES_FILE.name}"
                )
            env_document = ""
            if config_values.variables:
                env_document += f"# {VALUES_FILE.name}\n{render_env_entries(config_values.variables)}"
            if entries:
                env_document += f"# {LOCK_PATH.name}\n{render_env_entries(entries)}"
            with NamedTemporaryFile("w", suffix=".env", prefix="devkit-env-", delete=False, encoding="utf-8") as handle:
                handle.write(env_document)
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
            slot_names=[
                *config_values.variables,
                *entries,
                *(name for required in requirements.values() for name in required),
            ],
        )
        if not build:
            up_command += " --pull missing"
            if quiet_pull:
                up_command += " --quiet-pull"
        if not attached:
            up_command += " -d"

        bash_handoff(up_command)


def _producer_root(path: Path) -> Path:
    for ancestor in path.parents:
        if (ancestor / DEFAULT_CONFIG_PATH).is_file():
            return ancestor
    raise ValueError(f"{path.as_posix()}: no boatyard.yml-bearing repo root above the bundle override")
