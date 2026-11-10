from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from itertools import groupby
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

import typer
import yaml
from bashrun.bash import bash_handoff

from boatyard.build import run_build
from boatyard.compose import compose_command
from boatyard.models.config_file import (
    EDGE_LISTEN_PORT,
    GATEWAY_SERVICE_NAME,
    ConfigFile,
    double_dollars,
)
from boatyard.models.lock_file import LockFile
from boatyard.models.values_file import PUBLIC_PORT_SLOT, PUBLIC_SCHEME_SLOT, ValuesFile
from boatyard.paths import DEFAULT_CONFIG_PATH, LOCK_PATH, VALUES_FILE
from boatyard.resolve import resolve
from boatyard.yaml import Yaml

# Name the edge's Caddyfile configs key
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
    # Load the config, lock, and the typed machine values from the working directory
    root = Path.cwd()
    config = ConfigFile.load(root / DEFAULT_CONFIG_PATH)
    if not (root / VALUES_FILE).exists():
        raise ValueError(f"{VALUES_FILE.as_posix()} is required")
    lock = LockFile.load(config.root / LOCK_PATH, config)
    config_values = ValuesFile.load(root / VALUES_FILE)

    # One resolve answers the bundle projects, the gateway plan, the env table, and the edge image
    plan = resolve(config, lock, config_values)
    bundles = plan.bundles
    auth_entries = {row.name for row in config.service_entries if row.auth}

    # Group the ports under their namespaced owners into per-service stanzas, the typed
    # values overrides winning over the effective declarations
    document_services: dict[str, Yaml] = {
        f"{bundle.entry_name}-{owner}": {
            "ports": [
                {
                    **port.model_dump(exclude={"service"}, exclude_none=True),
                    "published": config_values.ports.get(owner, port.published),
                }
                for port in owner_ports
            ]
        }
        for bundle in bundles
        for owner, owner_ports in groupby(
            sorted(bundle.effective_ports, key=lambda port: port.service), key=lambda port: port.service
        )
    }

    # Assemble the root document, sections omitted when empty
    document: dict[str, Yaml] = {"name": config.project_name}

    # Render the edge and its Caddyfile when a gateway is declared
    if config.gateway_port is not None:
        if plan.edge_image is None:
            raise ValueError("a gateway is declared; boatyard.lock.yml lacks the boatyard-owned CADDY_IMAGE pin")
        gateway = config.gateway_port if config_values.gateway_port is None else config_values.gateway_port
        edge: dict[str, Yaml] = {
            "image": plan.edge_image,
            "configs": [{"source": EDGE_CONFIG_NAME, "target": "/etc/caddy/Caddyfile"}],
            "ports": [{"target": EDGE_LISTEN_PORT, "published": gateway, "protocol": "tcp"}],
            # Attach the edge to every selected bundle's network
            "networks": dict.fromkeys(f"{bundle.entry_name}-net" for bundle in bundles),
        }
        if any(bundle.entry_name in auth_entries for bundle in bundles):
            if config_values.public_host is None:
                raise ValueError("an auth option is selected; public_host is required in boatyard.values.yaml")
            edge["environment"] = {
                "FORWARDED_PROTO": plan.env[PUBLIC_SCHEME_SLOT],
                "FORWARDED_PORT": plan.env[PUBLIC_PORT_SLOT],
            }
        document_services[GATEWAY_SERVICE_NAME] = edge

        # Render one handle block per route in declaration order
        lines = [f":{EDGE_LISTEN_PORT} {{"]
        for bundle, index, route in (
            (bundle, index, route) for bundle in bundles for index, route in enumerate(bundle.effective_routes)
        ):
            upstream = f"{bundle.entry_name}-{route.upstream}"
            opener = (
                ("\thandle_path" if route.strip_prefix else "\thandle")
                + ("" if route.path == "/*" else f" {route.path}")
                + " {"
            )
            if route.raw is not None:
                lines.extend((opener, *["\t\t" + line for line in route.raw.splitlines()], "\t}"))
                continue

            if bundle.entry_name in auth_entries and index == 0:
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

    includes: list[Yaml] = [
        bundle.bundle_path.as_posix() if bundle.bundle_path is not None else f"oci://{bundle.pinned_reference}"
        for bundle in bundles
    ]
    if includes:
        document["include"] = includes
    if document_services:
        document["services"] = document_services

    with ExitStack() as stack:
        # Materialize the env file
        with NamedTemporaryFile("w", suffix=".env", prefix="devkit-env-", delete=False, encoding="utf-8") as handle:
            handle.write(render_env_entries(plan.env))
        materialized_env_file = Path(handle.name)
        stack.enter_context(lock_env_unwind(materialized_env_file))

        # Materialize the compose file
        with NamedTemporaryFile(
            "w", suffix=".yaml", prefix="devkit-assembly-", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        materialized_compose_file = Path(handle.name)
        stack.callback(materialized_compose_file.unlink, missing_ok=True)

        # Build local images
        if build:
            run_build(config=config)

        # Hand off to docker compose
        bash_handoff(
            compose_command(
                root,
                [materialized_compose_file],
                "up",
                *["--pull", "missing", *(["--quiet-pull"] if quiet_pull else []), *(["-d"] if not attached else [])],
                project_name=config.project_name,
                env_file=materialized_env_file,
                slot_names=list(plan.env),
            )
        )


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
