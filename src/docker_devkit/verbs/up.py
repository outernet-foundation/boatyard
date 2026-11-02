from __future__ import annotations

import re
import shlex
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import CalledProcessError, bash_handoff, bash_output

from docker_devkit.bake import run_build
from docker_devkit.compose import compose_command, merged_environment, render, require_env_file, select
from docker_devkit.config import DockerDevkitConfig, load_config

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


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

    # Select, render, and validate the invocation.
    with ExitStack() as stack:
        require_env_file(root)
        environment = merged_environment(root)
        selection = select(config, environment)
        assert config is not None
        invocation = render(root, config, environment, selection, stack, dev=dev)

        # Probe every lock entry before bring-up.
        _probe_lock_entries(config)

        # Build local images; --dev builds both shapes.
        if build:
            run_build(root=root, config=config)
            if dev:
                run_build(root=root, config=config, dev=True)

        # Hand off to docker compose up.
        up_command = compose_command(
            root,
            invocation.compose_files,
            *(argument for profile in invocation.profiles for argument in ("--profile", profile)),
            "up",
            lock_env_file=invocation.lock_env_file,
        )
        if not build:
            up_command += " --pull missing"
            if quiet_pull:
                up_command += " --quiet-pull"
        if not attached:
            up_command += " -d"

        bash_handoff(up_command)


def _probe_lock_entries(config: DockerDevkitConfig) -> None:
    lock = config.image_lock()
    references = sorted(
        set(
            [reference for entry in lock.images.values() for reference in entry.recorded_digests]
            + list(lock.charts.values())
        )
    )
    for value in references:
        reference, _, digest = value.rpartition("@")
        try:
            bash_output(f"docker buildx imagetools inspect {shlex.quote(value)}")
        except CalledProcessError as error:
            stderr = (error.stderr or str(error)).strip()
            if re.search(r"\b404\b|not found|manifest unknown", stderr, re.IGNORECASE):
                raise RuntimeError(f"Image {reference or value} (digest {digest or 'unknown'}) is not mirrored")
            raise RuntimeError(
                f"Image {reference or value} (digest {digest or 'unknown'}): the mirror probe failed: {stderr}"
            )
