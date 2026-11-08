from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from boatyard import images
from boatyard.config.config import ConfigFile
from boatyard.images import image_sha_env
from boatyard.paths import DEFAULT_CONFIG_PATH

app = typer.Typer()


@app.command()
def tree_tags(
    names: Annotated[
        list[str] | None,
        typer.Argument(help="Derived image names; each answers its HEAD tree tag, one per line."),
    ] = None,
) -> None:
    root = Path.cwd()
    config = ConfigFile.load(root / DEFAULT_CONFIG_PATH)
    vocabulary = sorted(config.images)
    if not names:
        raise typer.BadParameter(f"at least one image name is required; this repo derives {vocabulary or 'no images'}")
    unknown = [name for name in names if name not in config.images]
    if unknown:
        raise typer.BadParameter(f"unknown image(s) {unknown}; this repo derives {vocabulary}")
    shas = images.compute_image_shas(config.root, config.images)
    for name in names:
        print(shas[image_sha_env(name)])
