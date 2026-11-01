from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, TypeIs

import typer
import yaml
from bashrun.bash import CalledProcessError, bash_handoff, bash_output

from .assembly import Assembly, assert_known_profiles, require_compose_stack, require_env_file
from .build_docker import run_build
from .config import (
    CADDY_IMAGE,
    DEV_TOGGLES_ANNOTATION,
    DOOR_SERVICE_NAME,
    LOCK_PATH,
    DockerDevkitConfig,
    RouteConfig,
    ServiceGroup,
    compose_bundles,
    load_config,
    parse_dev_toggles,
    parse_image_lock,
)

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
    compose_files: list[Path]
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

    # Resolve once; every environment read and selection refusal rides the constructor.
    assembly = Assembly(root, config)
    assert config is not None

    # Render the dev overlay before the root, so the file list is final at one construction.
    dev_overlay: Path | None = None
    if dev:
        overlay = render_dev_overlay(root, config, assembly)
        with NamedTemporaryFile(
            "w", suffix=".yaml", prefix="devkit-dev-overlay-", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(yaml.safe_dump(overlay, sort_keys=False, default_flow_style=False))
            dev_overlay = Path(handle.name)

    # Write the per-run root and build the context over the final file list.
    assembly_root = _write_temp_root(render_root_document(config, assembly))
    compose_files = [assembly_root, dev_overlay] if dev_overlay is not None else [assembly_root]
    context = ComposeContext(root, compose_files, dev_overlay=dev_overlay, assembly_root=assembly_root)

    try:
        if assembly.routes or assembly.ports:
            # Validate every upstream and port owner against compose's own resolution.
            resolved = {
                line.strip() for line in bash_output(f"{context.command('config', '--services')}").splitlines()
            }
            resolved.discard("")
            for route in assembly.routes:
                upstream = route.upstream.partition(":")[0]
                if upstream not in resolved:
                    raise RuntimeError(
                        f"route {route.path} upstream {upstream} resolves to no service in the selected assembly"
                    )
            if assembly.auth_route is not None:
                upstream = assembly.auth_route.upstream.partition(":")[0]
                if upstream not in resolved:
                    raise RuntimeError(f"the selected auth option's upstream {upstream} is absent from the assembly")
            for port in assembly.ports:
                if port.service not in resolved:
                    raise RuntimeError(f"ports for service {port.service!r} — absent from the selected assembly")

        # Print the resolved assembly so nothing implicit rides hidden.
        if assembly.riding:
            print(f"riding: {', '.join(assembly.riding)}")
        for name, entry in config.services.items():
            if isinstance(entry, ServiceGroup):
                selected_option = assembly.choices.get(name)
                print(f"{name}: {selected_option if selected_option is not None else '(none selected)'}")
        if assembly.routes:
            print(f"door: {' '.join(route.path for route in assembly.routes)}")

        # Refuse profile names the compose files do not define.
        if assembly.profiles:
            defined = [
                line.strip()
                for line in bash_output(f"{context.command()} config --profiles").splitlines()
                if line.strip()
            ]
            assert_known_profiles(assembly.profiles, defined)

        # Refuse --build in a repo with no build declarations.
        if build and not (config.repository or config.base_images):
            raise typer.BadParameter(
                "--build requires build declarations (repository or base_images) in docker-devkit.yaml"
            )

        # Fail closed over every lock entry before bring-up.
        if context.lock_file.exists():
            _probe_lock_entries(context)

        # Build local images; --dev builds both shapes.
        if build:
            run_build()
            if dev:
                run_build(dev=True)

        # Hand off to docker compose up.
        up_command = context.command(*(f"--profile {profile}" for profile in assembly.profiles), "up")
        if not build:
            up_command += " --pull missing"
            if quiet_pull:
                up_command += " --quiet-pull"
        if not attached:
            up_command += " -d"

        bash_handoff(up_command)
    finally:
        _cleanup(context)


@down_app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    root = Path.cwd()
    config = load_config(root)

    require_env_file(root)
    require_compose_stack(config)
    assert config is not None

    # Tear down by project name, covering every selection and profile.
    assembly_root = _write_temp_root({"name": config.project_name})
    context = ComposeContext(root, [assembly_root], assembly_root=assembly_root)

    command = context.command("down", "--remove-orphans")
    if volumes:
        command += " -v"

    try:
        bash_handoff(command)
    finally:
        _cleanup(context)


def render_root_document(config: DockerDevkitConfig, assembly: Assembly) -> dict[str, object]:
    # The per-run root compose file: name, includes, the door, parent port stanzas, Caddyfile.
    assert config.project_name is not None
    services: dict[str, object] = {}
    if assembly.routes:
        assert config.gateway is not None
        services[DOOR_SERVICE_NAME] = {
            "image": CADDY_IMAGE_HOLE,
            "configs": [{"source": DOOR_CONFIG_NAME, "target": DOOR_CONFIG_TARGET}],
            "ports": [{"target": str(DOOR_LISTEN_PORT), "published": config.gateway, "protocol": "tcp"}],
            **({"environment": assembly.forwarded} if assembly.forwarded is not None else {}),
        }
    ordered_ports = sorted(assembly.ports, key=lambda port: port.service)
    services.update(
        {
            service: {
                "ports": [
                    {
                        "target": port.target,
                        "published": port.published,
                        "protocol": port.protocol,
                        **({"host_ip": port.host_ip} if port.host_ip is not None else {}),
                    }
                    for port in service_ports
                ]
            }
            for service, service_ports in groupby(ordered_ports, key=lambda port: port.service)
        }
    )
    document: dict[str, object] = {"name": config.project_name}
    if assembly.includes:
        document["include"] = assembly.includes
    if services:
        document["services"] = services
    if assembly.routes:
        document["configs"] = {DOOR_CONFIG_NAME: {"content": render_caddyfile(assembly.routes, assembly.auth_route)}}
    return document


def render_caddyfile(routes: list[RouteConfig], auth_route: RouteConfig | None) -> str:
    # One site on the internal port; each route one handle block, in declaration order
    # (caddy sorts them longest-path-first, so the order cannot shadow).
    lines = [f":{DOOR_LISTEN_PORT} {{"]
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
        lines.extend((f"{head}{matcher} {{", *block, "\t}"))
    lines.append("}")
    # Every $ doubles: compose interpolation would otherwise eat caddy's {$ENV} placeholders.
    return ("\n".join(lines) + "\n").replace("$", "$$")


def render_dev_overlay(root: Path, config: DockerDevkitConfig, assembly: Assembly) -> Mapping[str, object]:
    # The --dev overlay: dev-stage images with the repo bind, plus dev-toggle env holes.
    declared = assembly.output_services
    services: dict[str, dict[str, object]] = {
        name: {
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
        for name, service in config.service_builds.items()
        if service.dev_stage and name in declared
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
            if not isinstance(service_name, str) or not service_name or service_name not in declared:
                continue
            toggles = parse_dev_toggles(service_name, raw)
            service = services.setdefault(service_name, {})
            service["environment"] = {key: f"${{{key}:-{value}}}" for key, value in toggles.items()}
    if not services:
        raise RuntimeError(
            "--dev found no dev shape in the assembly: a service Dockerfile declaring a "
            f"dev stage or a workload carrying {DEV_TOGGLES_ANNOTATION}"
        )
    return {"services": services}


def _mapping_or_empty(value: object) -> dict[str, object]:
    return value if _is_mapping(value) else {}


def _write_temp_root(document: Mapping[str, Any]) -> Path:
    with NamedTemporaryFile("w", suffix=".yaml", prefix="devkit-assembly-", delete=False, encoding="utf-8") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=False, default_flow_style=False))
        return Path(handle.name)


def _probe_lock_entries(context: ComposeContext) -> None:
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


def _cleanup(context: ComposeContext) -> None:
    if context.dev_overlay is not None:
        context.dev_overlay.unlink(missing_ok=True)
    if context.assembly_root is not None:
        context.assembly_root.unlink(missing_ok=True)


def _is_mapping(value: Any) -> TypeIs[dict[str, object]]:
    return isinstance(value, dict)
