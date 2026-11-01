from __future__ import annotations

import difflib
import re
from pathlib import Path

import yaml

from docker_devkit.config import (
    CHART_REFERENCE_PREFIX,
    DEFAULT_MIRROR_PREFIX,
    DockerDevkitConfig,
    LOCK_PATH,
    ImageLock,
    ImageLockEntry,
    chart_lock_key,
    load_config,
    mirrored_ref,
    parse_image_lock,
)
from docker_devkit.image_refs import resolve_remote_digest, resolve_remote_manifests

DIGEST_TAIL_PATTERN = re.compile(r"@sha256:[a-f0-9]{64}$")


def relock(config: DockerDevkitConfig, *, upgrade: bool = False) -> ImageLock:
    # One structured entry per declaration: the mirror-prefixed reference carrying the
    # index digest plus one platform-manifest digest per declared platform, resolved
    # against upstream — crane copies are byte-identical, so the digests hold under both
    # names. The declared tag rides both reference forms, so a tag bump breaks the prefix
    # match and re-resolves while an unchanged declaration copies its frozen entry forward.
    # The computed lock owns exactly the declared names — orphans prune by construction.
    assert config.root is not None
    prefix = config.mirror.prefix if config.mirror is not None else DEFAULT_MIRROR_PREFIX
    committed = _committed_lock(config.root)

    images: dict[str, ImageLockEntry] = {}
    for name, reference in config.base_images.items():
        base = mirrored_ref(reference, prefix)
        if DIGEST_TAIL_PATTERN.search(reference):
            # A digest declaration names its exact bytes; it passes through prefixed-but-
            # unresolved and records uniformly as the index and every platform entry.
            images[name] = ImageLockEntry(index=base, platforms=dict.fromkeys(config.platforms, base))
        elif not upgrade and _declaration_frozen(committed.images.get(name), base, config.platforms):
            images[name] = committed.images[name]
        else:
            resolved = resolve_remote_manifests(reference, config.platforms, f"base_images entry {name}")
            images[name] = ImageLockEntry(
                index=f"{base}@{resolved.index_digest}",
                platforms={platform: f"{base}@{digest}" for platform, digest in resolved.platforms.items()},
            )

    charts: dict[str, str] = {}
    declared_charts = config.k8s.charts if config.k8s is not None else {}
    for chart_name, chart_reference in declared_charts.items():
        key = chart_lock_key(chart_name)
        reference = chart_reference.removeprefix(CHART_REFERENCE_PREFIX)
        base = mirrored_ref(reference, prefix)
        if DIGEST_TAIL_PATTERN.search(reference):
            charts[key] = base
        elif not upgrade and key in committed.charts and committed.charts[key].startswith(base + "@"):
            charts[key] = committed.charts[key]
        else:
            charts[key] = f"{base}@{resolve_remote_digest(reference)}"

    return ImageLock(images=images, charts=charts)


def render_lock(lock: ImageLock) -> str:
    # Deterministic render: sorted names, sorted platform keys, byte-stable across machines.
    document: dict[str, object] = {}
    if lock.images:
        document["images"] = {
            name: {
                "index": entry.index,
                **({"platforms": dict(sorted(entry.platforms.items()))} if entry.platforms else {}),
            }
            for name, entry in sorted(lock.images.items())
        }
    if lock.charts:
        document["charts"] = dict(sorted(lock.charts.items()))
    if not document:
        return "{}\n"
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=10_000_000)


def env_entries(lock: ImageLock) -> dict[str, str]:
    # The env projection of the lock: the flat NAME=reference vocabulary compose and bake
    # consume — the image entry answers by its index reference, the chart entry flat.
    return {**{name: entry.index for name, entry in lock.images.items()}, **lock.charts}


def write_lock(config: DockerDevkitConfig, *, upgrade: bool = False) -> ImageLock:
    lock = relock(config, upgrade=upgrade)
    assert config.root is not None
    (config.root / LOCK_PATH).write_text(render_lock(lock), encoding="utf-8")
    return lock


def run_lock(
    *,
    root: Path | None = None,
    config: DockerDevkitConfig | None = None,
    upgrade: bool = False,
    check: bool = False,
) -> None:
    root = root if root is not None else Path.cwd()
    config = config if config is not None else load_config(root)
    if config is None:
        print("no docker-devkit.yaml; no lock declarations to relock")
        return
    declared_charts = config.k8s.charts if config.k8s is not None else {}
    if not config.base_images and not declared_charts:
        print("no lock declarations (neither base_images nor k8s.charts); nothing to relock")
        return

    computed = render_lock(relock(config, upgrade=upgrade))
    assert config.root is not None
    lock_file = config.root / LOCK_PATH
    if check:
        committed = lock_file.read_text(encoding="utf-8") if lock_file.exists() else ""
        if committed != computed:
            print(
                "".join(
                    difflib.unified_diff(
                        committed.splitlines(keepends=True),
                        computed.splitlines(keepends=True),
                        fromfile=f"committed {LOCK_PATH.as_posix()}",
                        tofile=f"computed {LOCK_PATH.as_posix()}",
                    )
                )
            )
            raise SystemExit("image lock is stale. Run 'lock' and commit the result.")
        return
    lock_file.write_text(computed, encoding="utf-8")


def _committed_lock(root: Path) -> ImageLock:
    # The relock lanes read the committed lock leniently: an unparseable file (the env-line
    # format before the structured flip) re-resolves wholesale rather than refusing — the
    # rewrite is the cutover migration.
    path = root / LOCK_PATH
    if not path.is_file():
        return ImageLock()
    try:
        return parse_image_lock(path)
    except (TypeError, ValueError):
        print(f"{LOCK_PATH.as_posix()}: not the structured YAML format; re-resolving every entry")
        return ImageLock()


def _declaration_frozen(entry: ImageLockEntry | None, base: str, platforms: list[str]) -> bool:
    # The freeze key is the whole declaration the entry resolves under — the prefixed
    # reference and the declared platform set. Either changing re-resolves the entry.
    if entry is None:
        return False
    return entry.index.startswith(base + "@") and sorted(entry.platforms) == sorted(platforms)
