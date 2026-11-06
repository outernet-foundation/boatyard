from __future__ import annotations

import os
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from itertools import groupby
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

import typer
import yaml
from bashrun.bash import bash_handoff

from boatyard.bake import run_build
from boatyard.compose import compose_command
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
    scan_bundle_texts,
)
from boatyard.config.lock import LockFile
from boatyard.config.values import ValuesFile
from boatyard.paths import DEFAULT_CONFIG_PATH, LOCK_PATH, VALUES_FILE

# Name the edge listener port and its configs key
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
        typer.Option("--build", help="Build all images locally before bringing the stack up"),
    ] = False,
) -> None:
    # Load the config and the typed machine values from the working directory
    root = Path.cwd()
    config = ConfigFile.load(root / DEFAULT_CONFIG_PATH)
    if not (root / VALUES_FILE).exists():
        raise ValueError(f"{VALUES_FILE.as_posix()} is required")
    config_values = ValuesFile.load(root / VALUES_FILE, config)

    # Require the config to declare a compose stack
    if not config.services:
        raise ValueError("boatyard.yml must declare services")

    # Resolve the chosen options into leaf services
    services = config.resolve_services(config_values.choices)

    # Project the consumer lock's manifest references and seed the assembly accumulators
    lock = LockFile.load(config.root / LOCK_PATH, config)
    consumer_entries = lock.manifest_references(config_values.platform)
    entries: dict[str, str] = dict(consumer_entries)
    includes: list[Yaml] = []
    requirements: dict[str, list[str]] = {}

    # Collect each entry's include, requirements, and env entries in declaration order
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
            bundle_compose_path = compose_bundle_output_path(config.root, name)
            if not bundle_compose_path.is_file():
                raise ValueError(
                    f"selected entry {name!r} has no bundle output {bundle_compose_path.as_posix()}; generate-score writes it"
                )
            lock_references = consumer_entries
        else:
            # Include an overridden ref through its local path
            candidate = config_values.path_overrides[name]
            bundle_compose_path = (candidate if candidate.is_absolute() else config.root / candidate).resolve()
            if not bundle_compose_path.is_file():
                raise ValueError(f"path_overrides.{name}: {bundle_compose_path.as_posix()} does not exist")

            # Walk up from the override to the boatyard.yml-bearing producer root
            producer_root = next(
                (ancestor for ancestor in bundle_compose_path.parents if (ancestor / DEFAULT_CONFIG_PATH).is_file()),
                None,
            )
            if producer_root is None:
                raise ValueError(
                    f"{bundle_compose_path.as_posix()}: no boatyard.yml-bearing repo root above the bundle override"
                )

            # Project the producer lock's manifest references under the consumer's platform
            lock_references = LockFile.load(
                producer_root / LOCK_PATH, ConfigFile.load(producer_root / DEFAULT_CONFIG_PATH)
            ).manifest_references(config_values.platform)

        # Scan the bundle file for its requirements and refuse the reserved edge service
        text = bundle_compose_path.read_text(encoding="utf-8")
        scan = scan_bundle_texts(name, service.routes, service.ports, [text])
        if EDGE_SERVICE_NAME in scan.service_names:
            raise ValueError(
                f"{bundle_compose_path.as_posix()}: "
                f"the included bundle defines the reserved service {EDGE_SERVICE_NAME!r}"
            )

        # Record the entry's requirements, include the file by absolute path, and fan its lock keys under the prefix
        requirements[name] = scan.requires
        includes.append(bundle_compose_path.resolve().as_posix())
        entries.update({lock_key_prefix(name) + key: value for key, value in lock_references.items()})

    # Refuse bundles whose requirements no environment source fills, naming shell-set slots as ignored
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
    forwarded = None
    if any(service.auth for service in services):
        if config_values.public_url is None:
            raise ValueError("an auth option is selected; public_url is required in boatyard.values.yaml")
        forwarded = config_values.public_url.forwarded

    # Group the ports under their namespaced owners into per-service stanzas
    document_services: dict[str, Yaml] = {
        f"{service.name}-{owner}": {
            "ports": [port.model_dump(exclude={"service"}, exclude_none=True) for port in owner_ports]
        }
        for service in services
        for owner, owner_ports in groupby(
            sorted(service.ports, key=lambda port: port.service), key=lambda port: port.service
        )
    }

    # Assemble the root document, sections omitted when empty
    document: dict[str, Yaml] = {"name": config.project_name}
    if includes:
        document["include"] = includes

    # Render the edge and its Caddyfile when a gateway is declared
    if config.gateway is not None:
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
        for service, index, route in (
            (service, index, route) for service in services for index, route in enumerate(service.routes)
        ):
            upstream = f"{service.name}-{route.upstream}"
            opener = (
                ("\thandle_path" if route.strip_prefix else "\thandle")
                + ("" if route.path == "/*" else f" {route.path}")
                + " {"
            )
            if route.raw is not None:
                lines.extend((opener, *["\t\t" + line for line in route.raw.splitlines()], "\t}"))
                continue

            if service.auth and index == 0:
                lines.extend((
                    opener,
                    *[
                        f"\t\treverse_proxy {upstream} {{",
                        "\t\t\theader_up X-Forwarded-Proto {$FORWARDED_PROTO}",
                        "\t\t\theader_up X-Forwarded-Port {$FORWARDED_PORT}",
                        "\t\t}",
                    ],
                    "\t}",
                ))
                continue

            lines.extend((opener, *[f"\t\treverse_proxy {upstream}"], "\t}"))
        lines.append("}")
        document["configs"] = {EDGE_CONFIG_NAME: {"content": double_dollars("\n".join(lines) + "\n")}}

    if document_services:
        document["services"] = document_services

    # Materialize the per-run files and hand off
    with ExitStack() as stack:
        # Write the one per-run dotenv from consumer values and lock entries
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

        # Write the materialized root document to its per-run assembly file
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
        arguments = ["--pull", "missing", *(["--quiet-pull"] if quiet_pull else []), *(["-d"] if not attached else [])]
        up_command = compose_command(
            root,
            compose_files,
            "up",
            *arguments,
            env_file=env_file,
            slot_names=[
                *config_values.variables,
                *entries,
                *(name for required in requirements.values() for name in required),
            ],
        )

        bash_handoff(up_command)


def render_env_entries(entries: Mapping[str, str]) -> str:
    # Render the entries as sorted dotenv lines
    return "".join(f"{name}={reference}\n" for name, reference in sorted(entries.items()))


@contextmanager
def lock_env_unwind(path: Path) -> Generator[None]:
    # Keep the env file on failure echoing only its path, since contents may carry secrets, and unlink it on success
    try:
        yield
    except BaseException:
        print(f"env file kept for inspection: {path}")
        raise

    path.unlink(missing_ok=True)
