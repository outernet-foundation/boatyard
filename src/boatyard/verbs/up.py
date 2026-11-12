from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, NamedTuple

import typer
import yaml
from bashrun.bash import CalledProcessError, bash, bash_check

from boatyard.build import run_build
from boatyard.compose import compose_command
from boatyard.crane import install_crane
from boatyard.lock import OciArtifactManifest, extract_bundle_records
from boatyard.models.config_file import (
    EDGE_IMAGE,
    EDGE_LISTEN_PORT,
    GATEWAY_SERVICE_NAME,
    ConfigFile,
    PublishedPort,
    double_dollars,
)
from boatyard.models.lock_file import LockFile
from boatyard.models.values_file import ValuesFile
from boatyard.paths import DEFAULT_CONFIG_PATH, LOCK_PATH, VALUES_FILE
from boatyard.resolve import AssemblyPlan, BundleProject, GatewayPlan, resolve
from boatyard.yaml import Yaml

# Name the gateway's Caddyfile configs key
GATEWAY_CONFIG_NAME = "boatyard-gateway-caddyfile"

app = typer.Typer()


class BundleInvocation(NamedTuple):
    bundle: BundleProject
    compose_files: list[Path]


class UpContext(NamedTuple):
    env_file: Path
    invocations: list[BundleInvocation]
    gateway_file: Path | None


@app.command()
def up(
    quiet_pull: Annotated[
        bool,
        typer.Option("--quiet-pull", "-q", help="Suppress per-layer pull progress (still shows pull/push totals)."),
    ] = False,
    build: Annotated[
        bool,
        typer.Option("--build", help="Build all images locally before bringing the stack up"),
    ] = False,
) -> None:
    # Load the config, lock, and machine values from the working directory
    root = Path.cwd()
    config = ConfigFile.load(root / DEFAULT_CONFIG_PATH)

    # If the values file is missing, refuse
    if not (root / VALUES_FILE).exists():
        raise ValueError(f"{VALUES_FILE.as_posix()} is required")
    lock = LockFile.load(config.root / LOCK_PATH, config)
    config_values = ValuesFile.load(root / VALUES_FILE)

    # Resolve the assembly plan
    plan = resolve(config, lock, config_values)
    auth_entries = {row.name for row in config.service_entries if row.auth}
    shared_network_name = f"{config.project_name}-shared"
    materialized: list[Path] = []

    try:
        up_context = _materialize_up(config, config_values, plan, auth_entries, shared_network_name, materialized)
        _orchestrate_up(
            root,
            config,
            plan,
            up_context,
            quiet_pull=quiet_pull,
            build=build,
        )
    except CalledProcessError as error:
        # Exit naming the failing project, keeping the artifacts
        raise SystemExit(_retained_report(_failing_project(error), materialized)) from None
    except BaseException:
        # Print the kept artifacts, then re-raise
        print(_retained_report(None, materialized))
        raise

    # Remove the materialized files
    for path in materialized:
        path.unlink(missing_ok=True)


def _materialize_up(
    config: ConfigFile,
    config_values: ValuesFile,
    plan: AssemblyPlan,
    auth_entries: set[str],
    shared_network_name: str,
    materialized: list[Path],
) -> UpContext:
    # Materialize the env file every invocation shares
    with NamedTemporaryFile("w", suffix=".env", prefix="boatyard-env-", delete=False, encoding="utf-8") as handle:
        handle.write(render_env_entries(plan.env))
    env_file = Path(handle.name)
    materialized.append(env_file)

    # If a gateway is declared, materialize its document first
    gateway_file = (
        _materialize(
            materialized,
            "boatyard-gateway-",
            render_gateway_document(config.project_name, shared_network_name, plan, auth_entries),
        )
        if plan.gateway is not None
        else None
    )

    # Materialize each bundle's compose files and its override document
    invocations: list[BundleInvocation] = []
    for bundle in plan.bundles:
        # If the bundle is local, read its committed compose file
        if bundle.bundle_path is not None:
            compose_files = [bundle.bundle_path]
            declared_ports = extract_bundle_records([bundle.bundle_path.read_text(encoding="utf-8")]).declared_ports
        else:
            # Otherwise fetch the remote bundle's texts via crane
            install_crane()
            texts = OciArtifactManifest.fetch_compose_texts(bundle.pinned_reference or "")
            compose_files = [_materialize(materialized, "boatyard-bundle-", text) for text in texts]
            declared_ports = extract_bundle_records(texts).declared_ports
        override_file = _materialize(
            materialized,
            "boatyard-override-",
            render_override_document(shared_network_name, bundle, config_values, declared_ports),
        )
        invocations.append(BundleInvocation(bundle=bundle, compose_files=[*compose_files, override_file]))
    return UpContext(env_file=env_file, invocations=invocations, gateway_file=gateway_file)


def _orchestrate_up(
    root: Path,
    config: ConfigFile,
    plan: AssemblyPlan,
    up_context: UpContext,
    *,
    quiet_pull: bool,
    build: bool,
) -> None:
    # If any project exists, create the shared network first
    if up_context.invocations or up_context.gateway_file is not None:
        # If the network does not exist, create it
        if not bash_check(f"docker network inspect {config.project_name}-shared"):
            bash(f"docker network create {config.project_name}-shared")

    # If build is set, build the local images first
    if build:
        run_build(config=config)

    # Bring every bundle up as its own project, then the gateway project last
    up_arguments = ["up", "--pull", "missing", *(["--quiet-pull"] if quiet_pull else []), "-d"]
    for invocation in up_context.invocations:
        bash(
            compose_command(
                root,
                invocation.compose_files,
                *up_arguments,
                project_name=invocation.bundle.project_name,
                env_file=up_context.env_file,
                slot_names=list(plan.env),
            )
        )

    # If a gateway is declared, bring it up last
    if up_context.gateway_file is not None:
        bash(
            compose_command(
                root,
                [up_context.gateway_file],
                *up_arguments,
                project_name=config.project_name,
                env_file=up_context.env_file,
                slot_names=list(plan.env),
            )
        )


def _materialize(materialized: list[Path], prefix: str, text: str) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix=prefix, delete=False, encoding="utf-8") as handle:
        handle.write(text)
    path = Path(handle.name)
    materialized.append(path)
    return path


def _failing_project(error: CalledProcessError) -> str | None:
    # Read the project from the failing command's -p flag
    match = re.search(r"-p (\S+)", error.cmd if isinstance(error.cmd, str) else "")
    return match.group(1) if match is not None else None


def _retained_report(project: str | None, materialized: list[Path]) -> str:
    # Report the failure and the kept artifacts
    named = f"the project {project} failed; " if project is not None else "up failed; "
    return named + "kept artifacts for inspection: " + ", ".join(str(path) for path in materialized)


def render_env_entries(entries: Mapping[str, str]) -> str:
    # Render the entries as sorted dotenv lines
    return "".join(f"{name}={reference}\n" for name, reference in sorted(entries.items()))


def render_override_document(
    shared_network_name: str,
    bundle: BundleProject,
    values: ValuesFile,
    declared_ports: dict[str, list[tuple[int, int, str]]],
) -> str:
    # Hand-render the !reset spelling safe_dump cannot write
    lines = ["networks:", f"  {shared_network_name}:", "    external: true"]
    if bundle.services:
        lines.append("services:")
    for service_name in bundle.services:
        lines.extend((
            f"  {service_name}:",
            "    networks:",
            f"      {shared_network_name}:",
            f"        aliases: [{bundle.entry_name}-{service_name}]",
        ))
        ports = _override_ports(bundle, service_name, values, declared_ports)
        if ports:
            lines.append("    ports:")
            lines.extend(f"      - {_port_entry(port)}" for port in ports)
        if service_name in bundle.container_names:
            lines.append("    container_name: !reset null")
    return "\n".join(lines) + "\n"


def _override_ports(
    bundle: BundleProject,
    service_name: str,
    values: ValuesFile,
    declared_ports: dict[str, list[tuple[int, int, str]]],
) -> list[PublishedPort]:
    # Re-publish only the ports the bundle text does not declare
    declared = set(declared_ports.get(service_name, []))
    return [
        port.model_copy(update={"published": values.ports.get(service_name, port.published)})
        for port in bundle.effective_ports
        if port.service == service_name
        and (values.ports.get(service_name, port.published), port.target, port.protocol) not in declared
    ]


def _port_entry(port: PublishedPort) -> str:
    fields = [f"published: {port.published}", f"target: {port.target}"]
    if port.protocol != "tcp":
        fields.append(f"protocol: {port.protocol}")
    if port.host_ip is not None:
        fields.append(f"host_ip: {port.host_ip}")
    return "{" + ", ".join(fields) + "}"


def render_gateway_document(
    project_name: str,
    shared_network_name: str,
    plan: AssemblyPlan,
    auth_entries: set[str],
) -> str:
    gateway = _require_gateway(plan)

    # If an auth option is selected without a public face, refuse
    if any(bundle.entry_name in auth_entries for bundle in plan.bundles) and gateway.public_face is None:
        raise ValueError("an auth option is selected; public_host is required in boatyard.values.yaml")
    document: dict[str, Yaml] = {
        "name": project_name,
        "networks": {shared_network_name: {"external": True}},
        "services": {
            GATEWAY_SERVICE_NAME: {
                "image": EDGE_IMAGE,
                "configs": [{"source": GATEWAY_CONFIG_NAME, "target": "/etc/caddy/Caddyfile"}],
                "ports": [{"target": EDGE_LISTEN_PORT, "published": gateway.port, "protocol": "tcp"}],
                "networks": {shared_network_name: None},
            }
        },
        "configs": {GATEWAY_CONFIG_NAME: {"content": double_dollars(render_caddyfile(plan, gateway, auth_entries))}},
    }
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def render_caddyfile(plan: AssemblyPlan, gateway: GatewayPlan, auth_entries: set[str]) -> str:
    # Render one handle block per route in declaration order
    lines = [f":{EDGE_LISTEN_PORT} {{"]
    for bundle, index, route in (
        (bundle, index, route) for bundle in plan.bundles for index, route in enumerate(bundle.effective_routes)
    ):
        upstream = f"{bundle.entry_name}-{route.upstream}"
        opener = (
            ("\thandle_path" if route.strip_prefix else "\thandle")
            + ("" if route.path == "/*" else f" {route.path}")
            + " {"
        )
        # If the route carries a raw stanza, emit it verbatim
        if route.raw is not None:
            lines.extend((opener, *["\t\t" + line for line in route.raw.splitlines()], "\t}"))
            continue

        # If this is the auth route's first block, bake the public face as literals
        if bundle.entry_name in auth_entries and index == 0 and gateway.public_face is not None:
            face = gateway.public_face
            lines.extend((
                opener,
                *[
                    f"\t\treverse_proxy {upstream} {{",
                    f"\t\t\theader_up X-Forwarded-Proto {face.scheme}",
                    f"\t\t\theader_up X-Forwarded-Port {face.port}",
                    "\t\t}",
                ],
                "\t}",
            ))
            continue

        lines.extend((opener, *[f"\t\treverse_proxy {upstream}"], "\t}"))
    lines.append("}")
    return "\n".join(lines) + "\n"


def _require_gateway(plan: AssemblyPlan) -> GatewayPlan:
    if plan.gateway is None:
        raise ValueError("the gateway document renders only against a declared gateway")
    return plan.gateway
