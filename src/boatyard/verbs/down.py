from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

import typer
import yaml
from bashrun.bash import bash_handoff

from boatyard.compose import compose_command
from boatyard.config.config import ConfigFile, require_compose_stack
from boatyard.paths import DEFAULT_CONFIG_PATH

app = typer.Typer()


@app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    root = Path.cwd()
    config = require_compose_stack(ConfigFile.load(root / DEFAULT_CONFIG_PATH))

    # Tear down by project name alone; the name-only root consumes no values, lock, or selection.
    with ExitStack() as stack:
        with NamedTemporaryFile(
            "w", suffix=".yaml", prefix="devkit-assembly-", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(yaml.safe_dump({"name": config.project_name}, sort_keys=False, default_flow_style=False))
            assembly_root = Path(handle.name)
        stack.callback(assembly_root.unlink, missing_ok=True)

        command = compose_command(root, [assembly_root], "down", "--remove-orphans")
        if volumes:
            command += " -v"

        bash_handoff(command)
