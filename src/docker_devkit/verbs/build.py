from __future__ import annotations

from typing import Annotated

import typer

from docker_devkit.bake import run_build
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
    targets_opt: Annotated[
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
    targets = [address for value in targets_opt or [] for address in value.split()] or None
    run_build(push=push, targets_opt=targets, no_cache=no_cache, dev=dev)
