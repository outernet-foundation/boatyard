from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from os import environ
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any

import typer
import yaml
from bashrun.bash import CalledProcessError, bash_handoff, bash_output

from .build_docker import run_build
from .config import (
    AUTH_GROUP_NAME,
    DockerDevkitConfig,
    ENV_NAME_PATTERN,
    LOCK_PATH,
    RouteConfig,
    ServiceEntry,
    ServiceGroup,
    bundle_output,
    compose_bundles,
    load_config,
)
from .documents import parse_image_lock
from .host import parse_env_file

ENV_FILE = Path(".env")
DEFAULT_COMPOSE_FILE = Path("compose.yml")
BUNDLES_VARIABLE = "BUNDLES"
DEV_TOGGLES_ANNOTATION = "outernet.internal/dev-toggles"
DEV_MOUNT_TARGET = "/app"

DOOR_SERVICE_NAME = "door"
DOOR_LISTEN_PORT = 8443
DOOR_CONFIG_NAME = "devkit-door-caddyfile"
DOOR_CONFIG_TARGET = "/etc/caddy/Caddyfile"
CADDY_IMAGE_HOLE = "${CADDY_IMAGE:?err}"

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

    @classmethod
    def bring_up(cls, root: Path, *, dev: bool) -> ComposeContext:
        config = load_config(root)

        if not (root / ENV_FILE).exists():
            raise RuntimeError("No .env file found; create one first (e.g., copy .env.example)")

        environment = parse_env_file(root / ENV_FILE)
        environment.update(environ)
        names = _split_names(environment.get(BUNDLES_VARIABLE, ""))

        if config is None or not config.services:
            if names:
                raise RuntimeError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
            return cls(root, config, [DEFAULT_COMPOSE_FILE], environment)

        # BUNDLES= names options only: at most one per group, zero legal when riders carry
        # the assembly.
        options: dict[str, str] = {}
        riding: list[str] = []
        for name, entry in config.services.items():
            if isinstance(entry, ServiceGroup):
                for option_name in entry.options:
                    options[option_name] = name
            else:
                riding.append(name)
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

        # The include list in services declaration order: every riding entry plus each
        # group's selected option, as an OCI ref (remote) or an absolute bundle-output
        # path (local).
        sources: list[str] = []
        local_outputs: list[Path] = []
        for name, entry in entries:
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
            if DOOR_SERVICE_NAME in _service_names(root, output.relative_to(root)):
                raise RuntimeError(
                    f"bundle output {output.as_posix()} defines the reserved door service {DOOR_SERVICE_NAME!r}"
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

        routes = [route for _, entry in entries for route in entry.routes]
        ports = [port for _, entry in entries for port in entry.ports]

        auth_route: RouteConfig | None = None
        auth = config.services.get(AUTH_GROUP_NAME)
        if isinstance(auth, ServiceGroup):
            option_name = choices.get(AUTH_GROUP_NAME)
            if option_name is not None:
                auth_route = auth.options[option_name].routes[0]

        document: dict[str, Any] = {}
        if config.project_name:
            document["name"] = config.project_name
        if sources:
            document["include"] = sources

        services: dict[str, Any] = {}
        if routes:
            lock_path = root / LOCK_PATH
            if not lock_path.is_file() or "CADDY_IMAGE" not in parse_image_lock(lock_path).root:
                raise RuntimeError(
                    f"routes are declared, so the door rides; {LOCK_PATH.as_posix()} lacks the CADDY_IMAGE entry — "
                    "declare CADDY_IMAGE in base_images and run build --lock-only"
                )
            door_environment: dict[str, str] | None = None
            if auth_route is not None:
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
                door_environment = {"FORWARDED_PROTO": scheme, "FORWARDED_PORT": forwarded_port}
            assert config.gateway is not None
            stanza: dict[str, Any] = {
                "image": CADDY_IMAGE_HOLE,
                "configs": [{"source": DOOR_CONFIG_NAME, "target": DOOR_CONFIG_TARGET}],
                "ports": [{"target": str(DOOR_LISTEN_PORT), "published": config.gateway, "protocol": "tcp"}],
            }
            if door_environment:
                stanza["environment"] = door_environment
            services[DOOR_SERVICE_NAME] = stanza
        for port in ports:
            port_entry: dict[str, Any] = {"target": port.target, "published": port.published, "protocol": port.protocol}
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
        context = cls(root, config, [assembly_root], environment, assembly_root=assembly_root)
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

        if riding:
            print(f"riding: {', '.join(riding)}")
        for name, entry in config.services.items():
            if isinstance(entry, ServiceGroup):
                selected = choices.get(name)
                print(f"{name}: {selected if selected is not None else '(none selected)'}")
        if routes:
            print(f"door: {' '.join(route.path for route in routes)}")

        dev_overlay: Path | None = None
        if dev:
            declared: set[str] = set()
            for path in local_outputs:
                declared |= _service_names(root, path)

            overlay_services: dict[str, dict[str, Any]] = {}
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
                    workload_document: Any = yaml.safe_load(
                        (root / config.score_dir / workload).read_text(encoding="utf-8")
                    )
                    metadata: dict[str, Any] = workload_document.get("metadata") or {}
                    annotations: dict[str, Any] = metadata.get("annotations") or {}
                    raw: Any = annotations.get(DEV_TOGGLES_ANNOTATION)
                    if raw is None:
                        continue
                    if not isinstance(raw, str) or not raw.strip():
                        raise RuntimeError(
                            f"{workload}: {DEV_TOGGLES_ANNOTATION} must be a comma-separated NAME=default string"
                        )
                    service_name = metadata.get("name")
                    if not isinstance(service_name, str) or not service_name:
                        raise RuntimeError(f"{workload}: metadata.name is required to key the dev overlay service")
                    toggles: dict[str, str] = {}
                    for item in raw.split(","):
                        toggle_name, separator, default = item.strip().partition("=")
                        if not separator:
                            raise RuntimeError(
                                f"{workload}: {DEV_TOGGLES_ANNOTATION} entry {item.strip()!r} misses its default; "
                                "the ruled spelling is NAME=default pairs"
                            )
                        if ENV_NAME_PATTERN.fullmatch(toggle_name) is None:
                            raise RuntimeError(
                                f"{workload}: {DEV_TOGGLES_ANNOTATION} name {toggle_name!r} must be ALL_CAPS"
                            )
                        if not default:
                            raise RuntimeError(
                                f"{workload}: {DEV_TOGGLES_ANNOTATION} default for {toggle_name} is empty"
                            )
                        if toggle_name in toggles:
                            raise RuntimeError(f"{workload}: {DEV_TOGGLES_ANNOTATION} names {toggle_name} twice")
                        toggles[toggle_name] = default
                    if service_name not in declared:
                        continue
                    if "environment" in overlay_services.get(service_name, {}):
                        raise RuntimeError(
                            f"{workload} and another workload both declare {DEV_TOGGLES_ANNOTATION} for {service_name}"
                        )
                    overlay_services.setdefault(service_name, {})["environment"] = {
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

        return cls(
            root,
            config,
            [assembly_root, *([dev_overlay] if dev_overlay is not None else [])],
            environment,
            dev_overlay=dev_overlay,
            assembly_root=assembly_root,
        )

    @classmethod
    def teardown(cls, root: Path) -> ComposeContext:
        config = load_config(root)

        if not (root / ENV_FILE).exists():
            raise RuntimeError("No .env file found; create one first (e.g., copy .env.example)")

        if config is None or not config.services:
            return cls(root, config, [DEFAULT_COMPOSE_FILE], None)
        project_name = config.project_name
        if not project_name:
            raise RuntimeError(
                "docker-devkit.yaml declares no services; a builds-only or k8s-only repo has no compose stack"
            )

        # Teardown keys on the project, not on a file list: a name-only root plus
        # --remove-orphans removes every container the project ever created — any
        # selection, any profile — without resolving a single include, so an option's
        # env holes cannot break an open-mode teardown.
        assembly_root = _write_temp_root({"name": project_name})
        return cls(root, config, [assembly_root], None, assembly_root=assembly_root)

    @property
    def lock_file(self) -> Path:
        return self.root / LOCK_PATH

    def command(self, *arguments: str) -> str:
        files_arguments = " ".join(f"-f {compose_file}" for compose_file in self.compose_files)
        lock_argument = f" --env-file {self.lock_file}" if self.lock_file.exists() else ""
        prefix = f"docker compose {files_arguments} --project-directory {self.root} --env-file .env{lock_argument}"
        return " ".join([prefix, *arguments])


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
    context = ComposeContext.bring_up(root, dev=dev)

    if build and (context.config is None or not (context.config.repository or context.config.base_images)):
        raise typer.BadParameter(
            "--build requires build declarations (repository or base_images) in docker-devkit.yaml"
        )

    config = context.config
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
    profiles = _split_names((context.environment or {}).get("COMPOSE_PROFILES", ""))
    if profiles:
        defined = [
            line.strip() for line in bash_output(f"{context.command()} config --profiles").splitlines() if line.strip()
        ]
        unknown = [profile for profile in profiles if profile not in defined]
        if unknown:
            raise RuntimeError(
                f"COMPOSE_PROFILES selects unknown profile(s) {unknown}; "
                f"profiles defined by the compose files: {defined}"
            )
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
    context = ComposeContext.teardown(Path.cwd())
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


def _split_names(value: str) -> list[str]:
    names: list[str] = []
    for entry in value.split(","):
        name = entry.strip()
        if name and name not in names:
            names.append(name)
    return names


def _write_temp_root(document: dict[str, Any]) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix="devkit-assembly-", delete=False, encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        return Path(handle.name)


def _service_names(root: Path, path: Path) -> set[str]:
    # Name extraction only: the typed ComposeDocument lens carries bind laws generated
    # artifacts satisfy but authored compose files never promised to.
    document: Any = yaml.safe_load((root / path).read_text(encoding="utf-8"))
    services: dict[str, Any] = document.get("services") or {}
    return set(services)
