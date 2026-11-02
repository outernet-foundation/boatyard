from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from docker_devkit.config import load_config
from docker_devkit.dockerfiles import service_sha_env

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def tree_tags(
    services: Annotated[
        list[str] | None,
        typer.Argument(help="Derived service names; each answers its HEAD tree tag, one per line."),
    ] = None,
) -> None:
    root = Path.cwd()
    config = load_config(root)
    if config is None:
        raise RuntimeError("no docker-devkit.yaml at the repo root")
    vocabulary = sorted(config.service_builds)
    if not services:
        raise typer.BadParameter(
            f"at least one service name is required; this repo derives {vocabulary or 'no services'}"
        )
    unknown = [name for name in services if name not in config.service_builds]
    if unknown:
        raise typer.BadParameter(f"unknown service(s) {unknown}; this repo derives {vocabulary}")
    shas = config.service_shas()
    for name in services:
        print(shas[service_sha_env(name)])
