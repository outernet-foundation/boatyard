from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash_handoff

from boatyard.compose import compose_command
from boatyard.models.config_file import ConfigFile
from boatyard.paths import DEFAULT_CONFIG_PATH

app = typer.Typer()


@app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    root = Path.cwd()
    config = ConfigFile.load(root / DEFAULT_CONFIG_PATH)

    # Tear down by project name alone; the name-only invocation consumes no values, lock, or selection.
    bash_handoff(
        compose_command(
            root,
            [],
            "down",
            "--remove-orphans",
            *(["-v"] if volumes else []),
            project_name=config.project_name,
        )
    )
