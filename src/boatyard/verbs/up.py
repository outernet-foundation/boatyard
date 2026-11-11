from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, NamedTuple

import typer
import yaml
from bashrun.bash import CalledProcessError, bash, bash_check, bash_handoff, bash_output

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
from boatyard.up_display import WallDisplay
from boatyard.yaml import Yaml

# Name the gateway's Caddyfile configs key
GATEWAY_CONFIG_NAME = "boatyard-gateway-caddyfile"

app = typer.Typer()


class WorkerResult(NamedTuple):
    bundle: BundleProject
    ok: bool
    elapsed: float
    output: str
    error: CalledProcessError | ValueError | None
    paths: list[Path]


class MilestoneReport:
    # The one terminal-writing surface workers may use; every milestone prints under the lock.
    # Disabled while the interactive tree owns the terminal (non-TTY keeps the milestones).
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.lock = threading.Lock()

    def _emit(self, message: str) -> None:
        if self.enabled:
            with self.lock:
                print(message)

    def started(self, entry_name: str) -> None:
        self._emit(f"{entry_name}: up started")

    def ready(self, entry_name: str, elapsed: float) -> None:
        self._emit(f"{entry_name}: ready in {elapsed:.1f}s")

    def failed(self, entry_name: str, elapsed: float, error: CalledProcessError | ValueError) -> None:
        self._emit(f"{entry_name}: FAILED in {elapsed:.1f}s")
        if isinstance(error, CalledProcessError):
            for line in (error.output or "").splitlines():
                self._emit(f"  {line}")
        else:
            self._emit(f"  error: {error}")


class UpContext(NamedTuple):
    env_file: Path
    bundles: list[BundleProject]
    gateway_file: Path | None


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
    auth_entries = {row.name for row in config.service_entries if row.auth}
    shared_network_name = f"{config.project_name}-shared"
    materialized: list[Path] = []
    results: list[WorkerResult] = []

    try:
        up_context = _materialize_up(config, plan, auth_entries, shared_network_name, materialized)
        _orchestrate_up(
            root,
            config,
            plan,
            up_context,
            values=config_values,
            attached=attached,
            quiet_pull=quiet_pull,
            build=build,
            materialized=materialized,
            results=results,
        )
    except SystemExit:
        raise
    except CalledProcessError:
        raise SystemExit(_retained_report(None, materialized)) from None
    except BaseException:
        print(_retained_report(None, [*materialized, *_worker_paths(results)]))
        raise

    for path in materialized:
        path.unlink(missing_ok=True)


def _materialize_up(
    config: ConfigFile,
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

    # Materialize the gateway document when a gateway is declared, before any convergence starts
    # (the render's auth/public-face refusal must fire before anything is brought up)
    gateway_file = (
        _materialize(
            materialized,
            "boatyard-gateway-",
            render_gateway_document(config.project_name, shared_network_name, plan, auth_entries),
        )
        if plan.gateway is not None
        else None
    )

    return UpContext(env_file=env_file, bundles=list(plan.bundles), gateway_file=gateway_file)


def _orchestrate_up(
    root: Path,
    config: ConfigFile,
    plan: AssemblyPlan,
    up_context: UpContext,
    *,
    values: ValuesFile,
    attached: bool,
    quiet_pull: bool,
    build: bool,
    materialized: list[Path],
    results: list[WorkerResult],
) -> None:
    # Crane installed once on the main thread; N workers racing the installer's binary write
    if any(bundle.bundle_path is None for bundle in up_context.bundles):
        install_crane()

    # Create the shared network before any project when at least one project exists
    if up_context.bundles or up_context.gateway_file is not None:
        if not bash_check(f"docker network inspect {config.project_name}-shared"):
            bash(f"docker network create {config.project_name}-shared")

    # Build local images
    if build:
        run_build(config=config)

    # Every bundle converges on the wall; the gateway waits for the drained wall
    if up_context.bundles:
        _run_wall(
            root,
            plan,
            up_context,
            values=values,
            shared_network_name=f"{config.project_name}-shared",
            quiet_pull=quiet_pull,
            results=results,
        )
        failures = [result for result in results if not result.ok]
        for result in results:
            if result.ok:
                for path in result.paths:
                    path.unlink(missing_ok=True)
        if failures:
            raise SystemExit(_wall_report(failures, materialized))

    if up_context.gateway_file is not None:
        _run_gateway(
            root,
            config,
            plan,
            up_context.gateway_file,
            up_context.env_file,
            attached=attached,
            quiet_pull=quiet_pull,
            materialized=materialized,
        )


def _run_wall(
    root: Path,
    plan: AssemblyPlan,
    up_context: UpContext,
    *,
    values: ValuesFile,
    shared_network_name: str,
    quiet_pull: bool,
    results: list[WorkerResult],
) -> None:
    interactive = sys.stdout.isatty()
    report = MilestoneReport(enabled=not interactive)
    display = WallDisplay([bundle.project_name for bundle in up_context.bundles], active=interactive)
    threads = [
        threading.Thread(
            target=_up_bundle_project,
            args=(
                bundle,
                root,
                up_context.env_file,
                list(plan.env),
                values,
                shared_network_name,
                quiet_pull,
                _wall_runner,
                report,
                display,
                results,
            ),
        )
        for bundle in up_context.bundles
    ]
    for thread in threads:
        thread.start()
    try:
        display.run(threads)
        for thread in threads:
            thread.join()
    except BaseException:
        for thread in threads:
            thread.join(timeout=10)
        raise
    _replay_failures(results, enabled=interactive)


def _replay_failures(results: list[WorkerResult], *, enabled: bool) -> None:
    # Below the final tree: every failed project's captured stdout, two-space indented
    if not enabled:
        return
    for result in results:
        if not result.ok:
            print(f"{result.bundle.project_name}: failed")
            for line in result.output.splitlines():
                print(f"  {line}")


def _run_gateway(
    root: Path,
    config: ConfigFile,
    plan: AssemblyPlan,
    gateway_file: Path,
    env_file: Path,
    *,
    attached: bool,
    quiet_pull: bool,
    materialized: list[Path],
) -> None:
    arguments = ["up", "--pull", "missing", *(["--quiet-pull"] if quiet_pull else [])]
    try:
        if attached:
            bash_handoff(
                compose_command(
                    root,
                    [gateway_file],
                    *arguments,
                    project_name=config.project_name,
                    env_file=env_file,
                    slot_names=list(plan.env),
                )
            )
        bash(
            compose_command(
                root,
                [gateway_file],
                *arguments,
                "-d",
                project_name=config.project_name,
                env_file=env_file,
                slot_names=list(plan.env),
            )
        )
    except CalledProcessError as error:
        raise SystemExit(_retained_report(config.project_name, materialized)) from error


def _up_bundle_project(
    bundle: BundleProject,
    root: Path,
    env_file: Path,
    slot_names: list[str],
    values: ValuesFile,
    shared_network_name: str,
    quiet_pull: bool,
    runner: Callable[[str], str],
    report: MilestoneReport,
    display: WallDisplay,
    collect: list[WorkerResult],
) -> None:
    started = time.monotonic()
    report.started(bundle.entry_name)
    paths: list[Path] = []
    try:
        compose_files, declared_ports = _bundle_compose_files(bundle, paths)
        override_file = _materialize(
            paths,
            "boatyard-override-",
            render_override_document(shared_network_name, bundle, values, declared_ports),
        )
        output = runner(
            compose_command(
                root,
                [*compose_files, override_file],
                "--progress",
                "plain",
                "up",
                "--pull",
                "missing",
                *(["--quiet-pull"] if quiet_pull else []),
                "-d",
                project_name=bundle.project_name,
                env_file=env_file,
                slot_names=slot_names,
            )
        )
    except (CalledProcessError, ValueError) as error:
        elapsed = time.monotonic() - started
        report.failed(bundle.entry_name, elapsed, error)
        captured = (error.output or "") if isinstance(error, CalledProcessError) else ""
        for line in captured.splitlines():
            display.feed(bundle.project_name, line)
        display.finish(bundle.project_name, False, elapsed)
        collect.append(
            WorkerResult(bundle=bundle, ok=False, elapsed=elapsed, output=captured, error=error, paths=paths)
        )
        return
    elapsed = time.monotonic() - started
    for line in output.splitlines():
        display.feed(bundle.project_name, line)
    display.finish(bundle.project_name, True, elapsed)
    report.ready(bundle.entry_name, elapsed)
    collect.append(WorkerResult(bundle=bundle, ok=True, elapsed=elapsed, output=output, error=None, paths=paths))


def _bundle_compose_files(
    bundle: BundleProject, paths: list[Path]
) -> tuple[list[Path], dict[str, list[tuple[int, int, str]]]]:
    # A local path override rides its file directly; a remote bundle fetches its texts into temp files
    if bundle.bundle_path is not None:
        declared_ports = extract_bundle_records([bundle.bundle_path.read_text(encoding="utf-8")]).declared_ports
        return [bundle.bundle_path], declared_ports
    texts = OciArtifactManifest.fetch_compose_texts(bundle.pinned_reference or "")
    compose_files = [_materialize(paths, "boatyard-bundle-", text) for text in texts]
    return compose_files, extract_bundle_records(texts).declared_ports


def _wall_runner(command: str) -> str:
    return bash_output(command)


def _materialize(materialized: list[Path], prefix: str, text: str) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix=prefix, delete=False, encoding="utf-8") as handle:
        handle.write(text)
    path = Path(handle.name)
    materialized.append(path)
    return path


def _wall_report(failures: list[WorkerResult], materialized: list[Path]) -> str:
    # Completion-order failure lines; failed workers retain their paths alongside the shared artifacts
    lines = [f"the project {failure.bundle.project_name} failed" for failure in failures]
    retained = [*(path for failure in failures for path in failure.paths), *materialized]
    lines.append("kept artifacts for inspection: " + ", ".join(str(path) for path in retained))
    return "\n".join(lines)


def _retained_report(project: str | None, materialized: list[Path]) -> str:
    named = f"the project {project} failed; " if project is not None else "up failed; "
    return named + "kept artifacts for inspection: " + ", ".join(str(path) for path in materialized)


def _worker_paths(results: list[WorkerResult]) -> list[Path]:
    return [path for result in results for path in result.paths]


def render_env_entries(entries: Mapping[str, str]) -> str:
    # Render the entries as sorted dotenv lines
    return "".join(f"{name}={reference}\n" for name, reference in sorted(entries.items()))


def render_override_document(
    shared_network_name: str,
    bundle: BundleProject,
    values: ValuesFile,
    declared_ports: dict[str, list[tuple[int, int, str]]],
) -> str:
    # Hand-rendered because the document carries the !reset spelling safe_dump cannot write.
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
    # A declared port rides in the bundle's own text; the override re-publishes only the rest,
    # with the values scalar (single-port services only, validated at resolve) moving the host port.
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
    # One handle block per route in declaration order; the auth route's first block bakes the
    # effective public face as literals, never compose slots.
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
        if route.raw is not None:
            lines.extend((opener, *["\t\t" + line for line in route.raw.splitlines()], "\t}"))
            continue

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
