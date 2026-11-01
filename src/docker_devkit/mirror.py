from __future__ import annotations

from pathlib import Path

import typer
from bashrun.bash import bash, bash_check

from .config import (
    DEFAULT_MIRROR_PREFIX,
    chart_lock_key,
    load_config,
    optional_lock_entries,
    require_lock_entry,
)
from .crane import install_crane

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def mirror() -> None:
    root = Path.cwd()
    config = load_config(root)
    if config is None:
        raise RuntimeError("a docker-devkit.yaml with base_images (or k8s.charts) is required to mirror")
    prefix = config.mirror.prefix if config.mirror is not None else DEFAULT_MIRROR_PREFIX

    lock_entries = optional_lock_entries(root)
    mirror_targets = [
        (reference, lock_entries.get(name))
        for name, reference in sorted(config.base_images.items(), key=lambda item: item[1])
    ]
    for chart_name, chart_ref in sorted((config.k8s.charts if config.k8s is not None else {}).items()):
        entry = require_lock_entry(lock_entries, chart_lock_key(chart_name), f"chart {chart_name!r}")
        mirror_targets.append((chart_ref.removeprefix("oci://"), entry))

    if not mirror_targets:
        raise RuntimeError("nothing is declared to mirror (base_images and k8s.charts are empty)")

    install_crane()
    copied = 0
    already_present = 0
    for reference, presence in mirror_targets:
        if presence is not None and bash_check(f"crane digest {presence}"):
            print(f"Already mirrored {presence}")
            already_present += 1
            continue
        destination = mirrored_ref(reference, prefix)
        print(f"Mirror {reference} -> {destination}")
        bash(f"crane copy {reference} {destination}")
        copied += 1
    print(f"Mirrored references: {copied} copied, {already_present} already present")


def mirrored_ref(reference: str, prefix: str) -> str:
    if reference.startswith(f"{prefix}/"):
        raise RuntimeError(f"declarations are upstream-native; {reference} already carries the mirror prefix {prefix}")
    return f"{prefix}/{reference}"
