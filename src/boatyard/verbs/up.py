from __future__ import annotations

import re
import shlex
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import CalledProcessError, bash_handoff, bash_output

from boatyard.bake import run_build
from boatyard.compose import (
    BUNDLES_VARIABLE,
    compose_command,
    merged_environment,
    render,
    require_env_file,
    select,
    split_names,
)
from boatyard.config import Config, require_compose_stack
from boatyard.config_lock import read_config_lock
from boatyard.verbs.entry import entry

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
@entry
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
    config = Config.load(root)

    require_env_file(root)
    environment = merged_environment(root)
    if config is None or not config.services:
        if split_names(environment.get(BUNDLES_VARIABLE, "")):
            raise RuntimeError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
        config = require_compose_stack(config)

    # Refuse --build in a repo with no build declarations.
    if build and not (config.repository or config.base_images):
        raise typer.BadParameter(
            "--build requires build declarations (repository or base_images) in docker-devkit.yaml"
        )

    # Select, render, and validate the invocation.
    with ExitStack() as stack:
        selection = select(config, environment)
        invocation = render(root, config, environment, selection, stack, dev=dev)

        # Probe every lock entry before bring-up.
        _probe_lock_entries(config)

        # Build local images; --dev builds both shapes.
        if build:
            run_build(config=config)
            if dev:
                run_build(config=config, dev=True)

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


def _probe_lock_entries(config: Config) -> None:
    lock = read_config_lock(config.root)
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
