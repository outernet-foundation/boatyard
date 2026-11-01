from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash_handoff

from docker_devkit.assembly import (
    compose_command,
    keep_lock_env_file,
    merged_environment,
    require_compose_stack,
    require_env_file,
    write_lock_env_file,
    write_temp_compose_file,
)
from docker_devkit.config import load_config

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    root = Path.cwd()

    require_env_file(root)
    config = require_compose_stack(load_config(root))

    # Tear down by project name, covering every selection and profile.
    assembly_root = write_temp_compose_file({"name": config.project_name}, "devkit-assembly-")
    lock_env_file = write_lock_env_file(root, config, merged_environment(root))

    command = compose_command(root, [assembly_root], "down", "--remove-orphans", lock_env_file=lock_env_file)
    if volumes:
        command += " -v"

    try:
        bash_handoff(command)
    except BaseException:
        keep_lock_env_file(lock_env_file)
        lock_env_file = None
        raise
    finally:
        assembly_root.unlink(missing_ok=True)
        if lock_env_file is not None:
            lock_env_file.unlink(missing_ok=True)
