from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash_handoff

from docker_devkit.compose import (
    compose_command,
    merged_environment,
    require_env_file,
    write_lock_env_file,
    write_temp_compose_file,
)
from docker_devkit.config import load_config, require_compose_stack

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    root = Path.cwd()

    require_env_file(root)
    config = require_compose_stack(load_config(root))

    # Tear down by project name, covering every selection and profile.
    with ExitStack() as stack:
        lock_env_file = write_lock_env_file(root, config, merged_environment(root), stack)
        assembly_root = write_temp_compose_file(stack, {"name": config.project_name}, "devkit-assembly-")

        command = compose_command(root, [assembly_root], "down", "--remove-orphans", lock_env_file=lock_env_file)
        if volumes:
            command += " -v"

        bash_handoff(command)
