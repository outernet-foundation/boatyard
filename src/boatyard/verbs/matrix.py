from __future__ import annotations

import json
from pathlib import Path

import typer

from boatyard.config.config import ConfigFile
from boatyard.paths import DEFAULT_CONFIG_PATH

app = typer.Typer()


@app.command()
def docker_build_matrix() -> None:
    config_path = Path.cwd() / DEFAULT_CONFIG_PATH
    config = ConfigFile.load(config_path) if config_path.is_file() else None
    if config is None:
        print(json.dumps([]))
        return
    # Partition the images into dependency closures x variants.
    images = config.images
    parent = {image: image for image in images}

    for name, image in images.items():
        for dependency in image.contexts:
            left, right = _find(parent, name), _find(parent, dependency)
            if left != right:
                parent[right] = left

    members: dict[str, list[str]] = {}
    for image in images:
        members.setdefault(_find(parent, image), []).append(image)

    entries_by_variant: dict[tuple[str | None, str], list[str]] = {}
    for group in sorted(sorted(member_list) for member_list in members.values()):
        for name in group:
            for variant in images[name].variants or [None]:
                partition = variant if variant is not None else "common"
                address = f"{name}/{variant}" if variant is not None else name
                entries_by_variant.setdefault((images[name].axis, partition), []).append(address)

    print(
        json.dumps([
            {"targets": " ".join(sorted(addresses)), "variant": partition}
            for (_axis, partition), addresses in sorted(entries_by_variant.items(), key=lambda item: item[0][1])
        ])
    )


def _find(parent: dict[str, str], image: str) -> str:
    root = image
    while parent[root] != root:
        root = parent[root]
    while parent[image] != image:
        parent[image], image = root, parent[image]
    return root
