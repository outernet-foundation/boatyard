from __future__ import annotations

import json
from pathlib import Path

import typer

from docker_devkit.config import load_config

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def docker_build_matrix() -> None:
    config = load_config(Path.cwd())
    legs = config.build_legs() if config is not None else []
    print(json.dumps(legs))
