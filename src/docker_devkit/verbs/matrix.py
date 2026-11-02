from __future__ import annotations

import json
from pathlib import Path

import typer

from docker_devkit.config import load_config

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def docker_build_matrix() -> None:
    config = load_config(Path.cwd())
    if config is None:
        print(json.dumps([]))
        return
    # The CI fanout partitions: a union-find over the derived context edges groups
    # dependency closures, and load-time coherence guarantees one variant set per
    # closure, so one partition builds a whole closure group in a single bake
    # invocation; one partition per closure × variant cell. The cell schema
    # (targets, variant) is a named contract with the toolkit binding.
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
        json.dumps(
            [
                {"targets": " ".join(sorted(targets)), "variant": cell}
                for (_axis, cell), targets in sorted(entries_by_variant.items(), key=lambda item: item[0][1])
            ]
        )
    )


def _find(parent: dict[str, str], service: str) -> str:
    root = service
    while parent[root] != root:
        root = parent[root]
    while parent[service] != service:
        parent[service], service = root, parent[service]
    return root
