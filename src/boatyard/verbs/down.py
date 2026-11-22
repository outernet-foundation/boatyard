from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash, bash_check

from boatyard.models.config_file import ConfigFile
from boatyard.paths import DEFAULT_CONFIG_PATH
from boatyard.temp import quote_command_token

app = typer.Typer()


@app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    root = Path.cwd()
    config = ConfigFile.load(root / DEFAULT_CONFIG_PATH)

    # Config only: every declared entry names a candidate project, selected or not, and the
    # bare project name removes the un-suffixed project.
    candidates = [*(f"{config.project_name}-{row.name}" for row in config.service_entries), config.project_name]
    # Project and entry names match ^[a-z][a-z0-9-]*$; only the directory needs quoting
    project_directory = quote_command_token(str(root))
    volumes_argument = " -v" if volumes else ""
    for candidate in candidates:
        bash(
            f"docker compose -p {candidate} --project-directory {project_directory} "
            f"down --remove-orphans{volumes_argument}"
        )

    # The shared network comes down last, after every project that could attach to it
    shared_network_name = f"{config.project_name}-shared"
    if not bash_check(f"docker network inspect {shared_network_name}"):
        return
    bash(f"docker network rm {shared_network_name}")
