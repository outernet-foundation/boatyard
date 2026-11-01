from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, Literal, NotRequired, TypedDict, TypeIs

import typer
import yaml
from bashrun.bash import CalledProcessError, bash_handoff, bash_output

from .build_docker import run_build
from .config import (
    AUTH_GROUP_NAME,
    CADDY_IMAGE,
    DEV_TOGGLES_ANNOTATION,
    DOOR_SERVICE_NAME,
    DockerDevkitConfig,
    LOCK_PATH,
    RouteConfig,
    ServiceGroup,
    bundle_output,
    compose_bundles,
    load_config,
    parse_image_lock,
)
from .environment import (
    assert_known_profiles,
    forwarded_pair,
    merged_environment,
    require_env_file,
    selected_profiles,
    selection,
)

DEFAULT_COMPOSE_FILE = Path("compose.yml")
DEV_MOUNT_TARGET = "/app"

DOOR_LISTEN_PORT = 8443
DOOR_CONFIG_NAME = "devkit-door-caddyfile"
DOOR_CONFIG_TARGET = "/etc/caddy/Caddyfile"
CADDY_IMAGE_HOLE = f"${{{CADDY_IMAGE}:?err}}"

_MANIFEST_ABSENT_PATTERN = re.compile(r"\b404\b|not found|manifest unknown", re.IGNORECASE)

up_app = typer.Typer(add_completion=False)
down_app = typer.Typer(add_completion=False)


@dataclass(frozen=True)
class ComposeContext:
    root: Path
    config: DockerDevkitConfig | None
    compose_files: list[Path]
    environment: dict[str, str] | None
    dev_overlay: Path | None = None
    assembly_root: Path | None = None

    @property
    def lock_file(self) -> Path:
        return self.root / LOCK_PATH

    def command(self, *arguments: str) -> str:
        files_arguments = " ".join(f"-f {compose_file}" for compose_file in self.compose_files)
        lock_argument = f" --env-file {self.lock_file}" if self.lock_file.exists() else ""
        prefix = f"docker compose {files_arguments} --project-directory {self.root} --env-file .env{lock_argument}"
        return " ".join([prefix, *arguments])


class ConfigReference(TypedDict):
    source: str
    target: str


class PortEntry(TypedDict):
    target: str
    published: str
    protocol: str
    host_ip: NotRequired[str]


class InlineConfig(TypedDict):
    content: str


class ServiceStanza(TypedDict, total=False):
    image: str
    configs: list[ConfigReference]
    ports: list[PortEntry]
    environment: dict[str, str]


class DoorStanza(TypedDict):
    image: str
    configs: list[ConfigReference]
    ports: list[PortEntry]
    environment: NotRequired[dict[str, str]]


class DevMount(TypedDict):
    type: Literal["bind"]
    source: str
    target: str
    read_only: bool


class OverlayService(TypedDict, total=False):
    image: str
    volumes: list[DevMount]
    environment: dict[str, str]


class AssemblyRoot(TypedDict, total=False):
    name: str
    include: list[str]
    services: dict[str, ServiceStanza]
    configs: dict[str, InlineConfig]


@up_app.command()
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
    dev: Annotated[
        bool,
        typer.Option("--dev", help="Layer the generated dev overlay (dev-stage images, repo bind, .env toggle holes)."),
    ] = False,
) -> None:
    root = Path.cwd()
    config = load_config(root)

    environment = merged_environment(root)
    selected = selection(config, environment)

    if selected is None:
        context = ComposeContext(root, config, [DEFAULT_COMPOSE_FILE], environment)
    else:
        assert config is not None
        # The include list in services declaration order: every riding entry plus each
        # group's selected option, as an OCI ref (remote) or an absolute bundle-output
        # path (local).
        sources: list[str] = []
        local_outputs: list[Path] = []
        for name, entry in selected.entries:
            if isinstance(entry, ServiceGroup):
                continue
            if entry.ref is not None:
                sources.append(entry.ref)
                continue
            output = root / bundle_output(name)
            if not output.is_file():
                raise RuntimeError(
                    f"selected entry {name!r} has no bundle output {output.as_posix()}; generate-score writes it"
                )
            sources.append(output.resolve().as_posix())
            local_outputs.append(output)

        file_services = [(path, _service_names(root, path)) for path in local_outputs]
        for index, (path, service_names) in enumerate(file_services):
            for other_path, other_service_names in file_services[index + 1 :]:
                shared = sorted(service_names & other_service_names)
                if shared:
                    raise RuntimeError(
                        f"the assembly includes {path.as_posix()} and {other_path.as_posix()}, "
                        f"which both define service(s) {shared}; select one"
                    )

        routes = [route for _, entry in selected.entries for route in entry.routes]
        ports = [port for _, entry in selected.entries for port in entry.ports]

        auth_route: RouteConfig | None = None
        auth = config.services.get(AUTH_GROUP_NAME)
        if isinstance(auth, ServiceGroup):
            option_name = selected.choices.get(AUTH_GROUP_NAME)
            if option_name is not None:
                auth_route = auth.options[option_name].routes[0]

        document: AssemblyRoot = {}
        if config.project_name:
            document["name"] = config.project_name
        if sources:
            document["include"] = sources

        services: dict[str, ServiceStanza] = {}
        if routes:
            door_environment: dict[str, str] | None = None
            if auth_route is not None:
                door_environment = forwarded_pair(environment)
            assert config.gateway is not None
            stanza: DoorStanza = {
                "image": CADDY_IMAGE_HOLE,
                "configs": [{"source": DOOR_CONFIG_NAME, "target": DOOR_CONFIG_TARGET}],
                "ports": [{"target": str(DOOR_LISTEN_PORT), "published": config.gateway, "protocol": "tcp"}],
            }
            if door_environment:
                stanza["environment"] = door_environment
            services[DOOR_SERVICE_NAME] = ServiceStanza(**stanza)
        for port in ports:
            port_entry: PortEntry = {
                "target": port.target,
                "published": port.published,
                "protocol": port.protocol,
            }
            if port.host_ip is not None:
                port_entry["host_ip"] = port.host_ip
            services.setdefault(port.service, {}).setdefault("ports", []).append(port_entry)
        if services:
            document["services"] = services
        if routes:
            # The Caddyfile rides as inline content with every $ doubled, so compose
            # interpolation leaves caddy's {$ENV} runtime placeholders for the caddy runtime.
            # handle blocks are mutually exclusive and caddy sorts them longest-path-first,
            # so declaration order cannot shadow; the catch-all /* rides as a bare handle.
            caddyfile_lines = [f":{DOOR_LISTEN_PORT} {{"]
            for route in routes:
                matcher = "" if route.path == "/*" else f" {route.path}"
                if route.raw is not None:
                    block = ["\t\t" + line for line in route.raw.splitlines()]
                elif route is auth_route:
                    block = [
                        f"\t\treverse_proxy {route.upstream} {{",
                        "\t\t\theader_up X-Forwarded-Proto {$FORWARDED_PROTO}",
                        "\t\t\theader_up X-Forwarded-Port {$FORWARDED_PORT}",
                        "\t\t}",
                    ]
                else:
                    block = [f"\t\treverse_proxy {route.upstream}"]
                head = "\thandle_path" if route.strip_prefix else "\thandle"
                caddyfile_lines.extend((f"{head}{matcher} {{", *block, "\t}"))
            caddyfile_lines.append("}")
            document["configs"] = {
                DOOR_CONFIG_NAME: {"content": ("\n".join(caddyfile_lines) + "\n").replace("$", "$$")}
            }

        assembly_root = _write_temp_root(document)
        context = ComposeContext(root, config, [assembly_root], environment, assembly_root=assembly_root)
        if routes or ports:
            # The drift-impossible teeth, against the assembly compose itself resolves
            # (includes included): every route upstream and every port's owning service
            # must exist.
            try:
                resolved = {
                    line.strip() for line in bash_output(f"{context.command('config', '--services')}").splitlines()
                }
            except BaseException:
                assembly_root.unlink(missing_ok=True)
                raise

            resolved.discard("")
            for route in routes:
                upstream = route.upstream.partition(":")[0]
                if upstream not in resolved:
                    assembly_root.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"route {route.path} upstream {upstream} resolves to no service in the selected assembly"
                    )
            if auth_route is not None:
                upstream = auth_route.upstream.partition(":")[0]
                if upstream not in resolved:
                    assembly_root.unlink(missing_ok=True)
                    raise RuntimeError(f"the selected auth option's upstream {upstream} is absent from the assembly")
            for port in ports:
                if port.service not in resolved:
                    assembly_root.unlink(missing_ok=True)
                    raise RuntimeError(f"ports for service {port.service!r} — absent from the selected assembly")

        if selected.riding:
            print(f"riding: {', '.join(selected.riding)}")
        for name, entry in config.services.items():
            if isinstance(entry, ServiceGroup):
                selected_option = selected.choices.get(name)
                print(f"{name}: {selected_option if selected_option is not None else '(none selected)'}")
        if routes:
            print(f"door: {' '.join(route.path for route in routes)}")

        dev_overlay: Path | None = None
        if dev:
            declared: set[str] = set()
            for path in local_outputs:
                declared |= _service_names(root, path)

            overlay_services: dict[str, OverlayService] = {}
            for name, service in config.service_builds.items():
                if not service.dev_stage or name not in declared:
                    continue
                if config.repository is None:
                    raise RuntimeError("repository is required; dev image tags derive from it")
                overlay_services[name] = {
                    "image": f"{config.repository}/{name}:dev",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": root.resolve().as_posix(),
                            "target": DEV_MOUNT_TARGET,
                            "read_only": True,
                        }
                    ],
                }
            for bundle in compose_bundles(config):
                for workload in bundle.workloads:
                    workload_document: dict[str, object] = _mapping_or_empty(
                        yaml.safe_load((root / config.score_dir / workload).read_text(encoding="utf-8"))
                    )
                    metadata: dict[str, object] = _mapping_or_empty(workload_document.get("metadata"))
                    annotations: dict[str, object] = _mapping_or_empty(metadata.get("annotations"))
                    raw: object | None = annotations.get(DEV_TOGGLES_ANNOTATION)
                    if not isinstance(raw, str) or not raw.strip():
                        continue
                    service_name = metadata.get("name")
                    if not isinstance(service_name, str) or not service_name:
                        continue
                    if service_name not in declared:
                        continue
                    toggles = {
                        toggle_name: default
                        for toggle_name, _, default in (item.strip().partition("=") for item in raw.split(","))
                    }
                    overlay_services.setdefault(service_name, OverlayService())["environment"] = {
                        key: f"${{{key}:-{value}}}" for key, value in toggles.items()
                    }
            if not overlay_services:
                raise RuntimeError(
                    "--dev found no dev shape in the assembly: a service Dockerfile declaring a "
                    f"dev stage or a workload carrying {DEV_TOGGLES_ANNOTATION}"
                )
            with NamedTemporaryFile(
                "w", suffix=".yaml", prefix="devkit-dev-overlay-", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(yaml.safe_dump({"services": overlay_services}, sort_keys=False, default_flow_style=False))
                dev_overlay = Path(handle.name)

        context = ComposeContext(
            root,
            config,
            [assembly_root, *([dev_overlay] if dev_overlay is not None else [])],
            environment,
            dev_overlay=dev_overlay,
            assembly_root=assembly_root,
        )

    if build and (config is None or not (config.repository or config.base_images)):
        raise typer.BadParameter(
            "--build requires build declarations (repository or base_images) in docker-devkit.yaml"
        )

    if config is not None and context.lock_file.exists():
        # Fail closed over every lock entry — each mirror-prefixed by construction under
        # the declared or default prefix — distinguishing a manifest the registry truly
        # lacks (the declaring change has not been through CI's mirror job; no escape
        # flag) from a probe that failed before reaching a verdict (credentials, network,
        # daemon availability).
        for value in sorted(parse_image_lock(context.lock_file).root.values()):
            reference, _, digest = value.rpartition("@")
            try:
                bash_output(f"docker buildx imagetools inspect {shlex.quote(value)}")
            except CalledProcessError as error:
                stderr = (error.stderr or str(error)).strip()
                if _MANIFEST_ABSENT_PATTERN.search(stderr):
                    raise RuntimeError(
                        f"Image {reference or value} (digest {digest or 'unknown'}) is not mirrored yet; "
                        "the CI mirror job populates it when the declaring change runs"
                    )
                raise RuntimeError(
                    f"Image {reference or value} (digest {digest or 'unknown'}): the mirror probe "
                    "(docker buildx imagetools inspect) failed before it could determine whether the image "
                    "exists, so this is not evidence the image is missing; check registry credentials, network, "
                    f"and docker daemon availability. Probe error: {stderr}"
                )

    if build:
        run_build()
        if dev:
            # The overlay rides :dev images, so a dev bring-up builds both shapes.
            run_build(dev=True)

    # compose ignores COMPOSE_PROFILES whenever a --profile flag is present, so forward the
    # selection explicitly; shell env wins over .env, comma-split and deduplicated.
    profiles = selected_profiles(environment)
    if profiles:
        defined = [
            line.strip() for line in bash_output(f"{context.command()} config --profiles").splitlines() if line.strip()
        ]
        assert_known_profiles(profiles, defined)
    up_command = context.command(*(f"--profile {profile}" for profile in profiles), "up")
    if not build:
        # tree-<sha> tags are content-addressed and may not be pushed, so pull only what is missing locally
        up_command += " --pull missing"
        if quiet_pull:
            up_command += " --quiet-pull"
    if not attached:
        up_command += " -d"

    _handoff(context, up_command)


@down_app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    root = Path.cwd()
    config = load_config(root)

    require_env_file(root)

    if config is None or not config.services:
        context = ComposeContext(root, config, [DEFAULT_COMPOSE_FILE], None)
    else:
        project_name = config.project_name
        if not project_name:
            raise RuntimeError(
                "docker-devkit.yaml declares no services; a builds-only or k8s-only repo has no compose stack"
            )

        # Down keys on the project, not on a file list: a name-only root plus
        # --remove-orphans removes every container the project ever created — any
        # selection, any profile — without resolving a single include, so an option's
        # env holes cannot break an open-mode teardown.
        assembly_root = _write_temp_root({"name": project_name})
        context = ComposeContext(root, config, [assembly_root], None, assembly_root=assembly_root)

    command = context.command("down", "--remove-orphans")
    if volumes:
        command += " -v"

    _handoff(context, command)


def _handoff(context: ComposeContext, command: str) -> None:
    try:
        bash_handoff(command)
    finally:
        if context.dev_overlay is not None:
            context.dev_overlay.unlink(missing_ok=True)
        if context.assembly_root is not None:
            context.assembly_root.unlink(missing_ok=True)


def _write_temp_root(document: Mapping[str, Any]) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix="devkit-assembly-", delete=False, encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        return Path(handle.name)


def _service_names(root: Path, path: Path) -> set[str]:
    # Name extraction only: the typed ComposeDocument lens carries bind laws generated
    # artifacts satisfy but authored compose files never promised to.
    document: dict[str, object] = _mapping_or_empty(yaml.safe_load((root / path).read_text(encoding="utf-8")))
    return set(_mapping_or_empty(document.get("services")))


def _mapping_or_empty(value: object) -> dict[str, object]:
    return value if _is_mapping(value) else {}


def _is_mapping(value: Any) -> TypeIs[dict[str, object]]:
    return isinstance(value, dict)
