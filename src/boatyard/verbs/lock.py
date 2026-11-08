from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from boatyard.config.config import ConfigFile
from boatyard.lock import relock
from boatyard.paths import DEFAULT_CONFIG_PATH

app = typer.Typer()


@app.command()
def lock(
    upgrade: Annotated[
        bool,
        typer.Option("--upgrade", "-u", help="Re-resolve every declaration instead of freezing unchanged entries."),
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Assert the committed lock is fresh instead of writing it."),
    ] = False,
) -> None:
    config_path = Path.cwd() / DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        print("no boatyard.yml; no lock declarations to relock")
        return
    config = ConfigFile.load(config_path)
    if not (config.base_images or config.owned_images or config.declared_charts or config.remote_declared_entries()):
        print("no lock declarations (none of base_images, k8s.charts, service refs); nothing to relock")
        return
    relock(config, upgrade=upgrade, check=check)
