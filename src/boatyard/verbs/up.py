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
    compose_command,
    render_compose,
    require_env_file,
)
from boatyard.config import Config, bundle_lock_key, require_compose_stack
from boatyard.config_lock import read_config_lock
from boatyard.crane import install_crane
from boatyard.environment import BUNDLES_VARIABLE, Environment

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
        typer.Option("--build", help="Build all images locally before bringing the stack up; skips pulling"),
    ] = False,
) -> None:
    root = Path.cwd()
    config = Config.load(root)

    require_env_file(root)
    environment = Environment.load(root)
    if config is None or not config.services:
        if environment.bundles:
            raise ValueError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
        config = require_compose_stack(config)

    # Refuse --build in a repo with no build declarations.
    if build and not (config.repository or config.base_images):
        raise typer.BadParameter("--build requires build declarations (repository or base_images) in boatyard.yml")

    # Render and validate the invocation.
    with ExitStack() as stack:
        invocation = render_compose(config, environment, stack)

        # Probe every lock entry before bring-up; an overridden bundle skips its probe —
        # testing a bundle not published at any digest yet is the override's point.
        _probe_lock_entries(config, {override.name for override in invocation.overrides})

        # Build local images.
        if build:
            run_build(config=config)

        # Hand off to docker compose up.
        up_command = compose_command(
            root,
            invocation.compose_files,
            *(argument for profile in invocation.profiles for argument in ("--profile", profile)),
            "up",
            env_file=invocation.env_file,
        )
        if not build:
            up_command += " --pull missing"
            if quiet_pull:
                up_command += " --quiet-pull"
        if not attached:
            up_command += " -d"

        bash_handoff(up_command)


def _probe_lock_entries(config: Config, overridden: set[str]) -> None:
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
                raise ValueError(f"Image {reference or value} (digest {digest or 'unknown'}) is not mirrored")
            raise ValueError(
                f"Image {reference or value} (digest {digest or 'unknown'}): the mirror probe failed: {stderr}"
            )

    skip = {bundle_lock_key(name) for name in overridden}
    bundle_references = sorted({value for key, value in lock.bundles.items() if key not in skip})
    if not bundle_references:
        return
    # Compose-bundle media types are not image indexes; imagetools may refuse them, so bundle
    # entries probe via crane under the same stderr-classification law.
    install_crane()
    for value in bundle_references:
        reference, _, digest = value.rpartition("@")
        try:
            bash_output(f"crane digest {shlex.quote(value)}")
        except CalledProcessError as error:
            stderr = (error.stderr or str(error)).strip()
            if re.search(r"\b404\b|not found|manifest unknown", stderr, re.IGNORECASE):
                raise ValueError(f"Bundle {reference} (digest {digest}) is not published; run boatyard lock")
            raise ValueError(f"Bundle {reference} (digest {digest}): the bundle probe failed: {stderr}")
