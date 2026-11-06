from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from boatyard.build import run_build
from boatyard.config.config import ConfigFile
from boatyard.paths import DEFAULT_CONFIG_PATH

app = typer.Typer()


@app.command()
def build(
    push: Annotated[
        bool, typer.Option("--push", help="Push images and registry caches to the repository declared in the config.")
    ] = False,
    push_to: Annotated[
        str | None,
        typer.Option("--push-to", help="Push images and registry caches to this registry address instead."),
    ] = None,
    targets: Annotated[
        list[str] | None,
        typer.Option(
            "--targets",
            "-t",
            help="Build these targets: bare names select default variants, name/variant a declared variant.",
        ),
    ] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Force rebuild by disabling cache usage.")] = False,
) -> None:
    root = Path.cwd()
    config = ConfigFile.load(root / DEFAULT_CONFIG_PATH)

    if push and push_to is not None:
        raise typer.BadParameter(
            "--push and --push-to are mutually exclusive: --push targets the config's repository, --push-to targets that address"
        )

    if push_to is not None:
        registry_host = push_to.partition("/")[0]
        if "://" in push_to or push_to.endswith("/") or ("." not in registry_host and ":" not in registry_host):
            raise typer.BadParameter(
                f"--push-to destination {push_to!r} must be a registry address (localhost:5000, ghcr.io/org) "
                "with no scheme and no trailing slash"
            )

    if push and config.repository is None:
        raise typer.BadParameter("--push targets the config's repository; repository is required")

    targets = [address for value in targets or [] for address in value.split()] or None
    run_build(config=config, push=push, push_to=push_to, targets=targets, no_cache=no_cache)
