from __future__ import annotations

from pathlib import Path

import typer

from docker_devkit.assembly import merged_environment
from docker_devkit.config import LOCK_PATH, load_config, parse_image_lock
from docker_devkit.lock import compose_env_entries, render_env_entries

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def lock_env() -> None:
    # The raw-docker compose valve: stdout carries only the synthesized env lines, so
    # `docker compose --env-file <(uv run lock-env) …` consumes it directly. Repos with
    # no lock record answer empty output.
    root = Path.cwd()
    config = load_config(root)
    lock_path = root / LOCK_PATH
    if config is None or not lock_path.is_file():
        return
    entries = compose_env_entries(parse_image_lock(lock_path), config, merged_environment(root))
    print(render_env_entries(entries), end="")
