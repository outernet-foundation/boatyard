from __future__ import annotations

import json
from pathlib import Path

import typer

from boatyard.config.config import Config
from boatyard.paths import DEFAULT_CONFIG_PATH

app = typer.Typer()


@app.command()
def docker_build_matrix() -> None:
    config_path = Path.cwd() / DEFAULT_CONFIG_PATH
    config = Config.load(config_path) if config_path.is_file() else None
    if config is None:
        print(json.dumps([]))
        return
    # Partition the services into dependency closures x variant cells.
    services = config.service_builds
    parent = {service: service for service in services}

    for name, service in services.items():
        for dependency in service.contexts:
            left, right = _find(parent, name), _find(parent, dependency)
            if left != right:
                parent[right] = left

    members: dict[str, list[str]] = {}
    for service in services:
        members.setdefault(_find(parent, service), []).append(service)

    entries_by_variant: dict[tuple[str | None, str], list[str]] = {}
    for group in sorted(sorted(member_list) for member_list in members.values()):
        for name in group:
            for target, variant in services[name].target_variants:
                cell = variant if variant is not None else "common"
                entries_by_variant.setdefault((services[name].axis, cell), []).append(target)

    print(
        json.dumps([
            {"targets": " ".join(sorted(targets)), "variant": cell}
            for (_axis, cell), targets in sorted(entries_by_variant.items(), key=lambda item: item[0][1])
        ])
    )


def _find(parent: dict[str, str], service: str) -> str:
    root = service
    while parent[root] != root:
        root = parent[root]
    while parent[service] != service:
        parent[service], service = root, parent[service]
    return root
