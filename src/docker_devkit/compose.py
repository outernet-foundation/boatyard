from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import CalledProcessError, bash_handoff, bash_output

from docker_devkit.assembly import (
    Assembly,
    compose_command,
    require_compose_stack,
    require_env_file,
    write_temp_compose_file,
)
from docker_devkit.build_docker import run_build
from docker_devkit.config import load_config, optional_lock_entries

_MANIFEST_ABSENT_PATTERN = re.compile(r"\b404\b|not found|manifest unknown", re.IGNORECASE)

up_app = typer.Typer(add_completion=False)
down_app = typer.Typer(add_completion=False)


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

    # Refuse --build in a repo with no build declarations.
    if config is not None and build and not (config.repository or config.base_images):
        raise typer.BadParameter(
            "--build requires build declarations (repository or base_images) in docker-devkit.yaml"
        )

    # Resolve, render, and validate in one construction; every refusal rides it.
    assembly = Assembly(root, config, dev=dev)

    try:
        # Fail closed over every lock entry before bring-up.
        _probe_lock_entries(root)

        # Build local images; --dev builds both shapes.
        if build:
            run_build(root=root, config=assembly.config)
            if dev:
                run_build(root=root, config=assembly.config, dev=True)

        # Hand off to docker compose up.
        up_command = compose_command(
            root,
            assembly.compose_files,
            *(argument for profile in assembly.profiles for argument in ("--profile", profile)),
            "up",
        )
        if not build:
            up_command += " --pull missing"
            if quiet_pull:
                up_command += " --quiet-pull"
        if not attached:
            up_command += " -d"

        bash_handoff(up_command)
    finally:
        assembly.cleanup()


@down_app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    root = Path.cwd()

    require_env_file(root)
    config = require_compose_stack(load_config(root))

    # Tear down by project name, covering every selection and profile.
    assembly_root = write_temp_compose_file({"name": config.project_name}, "devkit-assembly-")

    command = compose_command(root, [assembly_root], "down", "--remove-orphans")
    if volumes:
        command += " -v"

    try:
        bash_handoff(command)
    finally:
        assembly_root.unlink(missing_ok=True)


def _probe_lock_entries(root: Path) -> None:
    for value in sorted(optional_lock_entries(root).values()):
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
