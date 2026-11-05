from __future__ import annotations

from pathlib import Path

import typer
from bashrun.bash import bash, bash_check

from boatyard.config.config import (
    ConfigFile,
    chart_lock_key,
    mirrored_ref,
)
from boatyard.crane import install_crane
from boatyard.config.lock import read_config_lock
from boatyard.paths import DEFAULT_CONFIG_PATH

app = typer.Typer()


@app.command()
def mirror() -> None:
    root = Path.cwd()
    config = ConfigFile.load(root / DEFAULT_CONFIG_PATH)
    prefix = config.mirror_prefix

    lock = read_config_lock(root)
    mirror_targets: list[tuple[str, list[str]]] = []
    for name, reference in sorted(config.base_images.items(), key=lambda item: item[1]):
        entry = lock.images.get(name)
        recorded = [] if entry is None else entry.recorded_digests
        mirror_targets.append((reference, recorded))
    for chart_name, chart_ref in sorted(config.declared_charts.items()):
        entry = lock.require(chart_lock_key(chart_name), f"chart {chart_name!r}")
        mirror_targets.append((chart_ref.removeprefix("oci://"), [entry]))

    if not mirror_targets:
        raise ValueError("nothing is declared to mirror (base_images and k8s.charts are empty)")

    install_crane()
    copied = 0
    already_present = 0
    for reference, recorded in mirror_targets:
        probes = sorted(set(recorded))
        if probes and all(bash_check(f"crane digest {probe}") for probe in probes):
            for probe in probes:
                print(f"Already mirrored {probe}")
            already_present += 1
            continue
        destination = mirrored_ref(reference, prefix)
        print(f"Mirror {reference} -> {destination}")
        bash(f"crane copy {reference} {destination}")
        copied += 1
    print(f"Mirrored references: {copied} copied, {already_present} already present")
