from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash, bash_check

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

    # Config only: every declared entry names a candidate project, selected or not, and the
    # bare project name also removes any legacy single-project assembly.
    candidates = [*(f"{config.project_name}-{row.name}" for row in config.service_entries), config.project_name]
    for candidate in candidates:
        bash(
            compose_command(
                root,
                [],
                "down",
                "--remove-orphans",
                *(["-v"] if volumes else []),
                project_name=candidate,
            )
        )

    # The shared network comes down last, after every project that could attach to it
    shared_network_name = f"{config.project_name}-shared"
    if not bash_check(f"docker network inspect {shared_network_name}"):
        return
    bash(f"docker network rm {shared_network_name}")
