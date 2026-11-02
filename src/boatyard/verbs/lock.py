from __future__ import annotations

from typing import Annotated

import typer

from boatyard.lock import run_lock

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


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
    run_lock(upgrade=upgrade, check=check)
