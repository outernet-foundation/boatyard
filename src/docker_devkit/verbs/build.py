from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from docker_devkit.bake import run_build
from docker_devkit.config import Config
from docker_devkit.optional_value_option import DestinationCommand

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command(cls=DestinationCommand)
def build(
    push: Annotated[
        str | None,
        typer.Option(
            "--push",
            help="Push images and registry caches: alone builds for the config's repository, --push <addr> for that registry.",
        ),
    ] = None,
    targets: Annotated[
        list[str] | None,
        typer.Option(
            "--targets", "-t", help="Build these targets: bare names select default cells, name/cell a declared cell."
        ),
    ] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Force rebuild by disabling cache usage.")] = False,
    dev: Annotated[
        bool, typer.Option("--dev", help="Build each Dockerfile-declared dev stage under the :dev tag (local-only).")
    ] = False,
) -> None:
    root = Path.cwd()
    config = Config.load(root)
    if config is None:
        raise RuntimeError("No docker-devkit.yaml at the repo root")

    if dev and push is not None:
        raise typer.BadParameter("--dev builds local-only images; --push is not allowed")

    if push:
        registry_host = push.partition("/")[0]
        if "://" in push or push.endswith("/") or ("." not in registry_host and ":" not in registry_host):
            raise typer.BadParameter(
                f"--push destination {push!r} must be a registry address (localhost:5000, ghcr.io/org) "
                "with no scheme and no trailing slash"
            )
    elif push is not None and config.repository is None:
        raise typer.BadParameter("--push is declared with no address; repository is required")

    targets = [address for value in targets or [] for address in value.split()] or None
    run_build(config=config, push=push, targets=targets, no_cache=no_cache, dev=dev)
