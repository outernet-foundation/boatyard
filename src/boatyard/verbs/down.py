from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

import typer
import yaml
from bashrun.bash import bash_handoff

from boatyard.compose import compose_command, lock_env_unwind, require_env_file
from boatyard.config_file import Config, require_compose_stack
from boatyard.lock import compose_env_entries, render_env_entries
from boatyard.lock_file import read_config_lock
from boatyard.values_file import Environment

app = typer.Typer()


@app.command()
def down(
    volumes: Annotated[bool, typer.Option("--volumes", "-v", help="Remove named volumes.")] = False,
) -> None:
    root = Path.cwd()

    require_env_file(root)
    config = require_compose_stack(Config.load(root))

    # Tear down by project name, covering every selection.
    with ExitStack() as stack:
        environment = Environment.load(root, config)
        entries = compose_env_entries(read_config_lock(root), config, environment.platform)
        env_file: Path | None = None
        if entries:
            with NamedTemporaryFile(
                "w", suffix=".env", prefix="devkit-lock-env-", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(render_env_entries(entries))
                env_file = Path(handle.name)
            stack.enter_context(lock_env_unwind(env_file))
        with NamedTemporaryFile(
            "w", suffix=".yaml", prefix="devkit-assembly-", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(yaml.safe_dump({"name": config.project_name}, sort_keys=False, default_flow_style=False))
            assembly_root = Path(handle.name)
        stack.callback(assembly_root.unlink, missing_ok=True)

        command = compose_command(root, [assembly_root], "down", "--remove-orphans", env_file=env_file)
        if volumes:
            command += " -v"

        bash_handoff(command)
