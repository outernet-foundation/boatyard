from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from boatyard import dockerfiles
from boatyard.config.config import Config
from boatyard.dockerfiles import service_sha_env
from boatyard.paths import DEFAULT_CONFIG_PATH

app = typer.Typer()


@app.command()
def tree_tags(
    services: Annotated[
        list[str] | None,
        typer.Argument(help="Derived service names; each answers its HEAD tree tag, one per line."),
    ] = None,
) -> None:
    root = Path.cwd()
    config = Config.load(root / DEFAULT_CONFIG_PATH)
    vocabulary = sorted(config.service_builds)
    if not services:
        raise typer.BadParameter(
            f"at least one service name is required; this repo derives {vocabulary or 'no services'}"
        )
    unknown = [name for name in services if name not in config.service_builds]
    if unknown:
        raise typer.BadParameter(f"unknown service(s) {unknown}; this repo derives {vocabulary}")
    shas = dockerfiles.compute_service_shas(config.root, config.service_builds)
    for name in services:
        print(shas[service_sha_env(name)])
