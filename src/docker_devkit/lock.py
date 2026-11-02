from __future__ import annotations

import difflib
import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

import yaml
from bashrun.bash import bash_output

from docker_devkit.config import (
    CHART_REFERENCE_PREFIX,
    DockerDevkitConfig,
    chart_lock_key,
    is_list,
    is_mapping,
    mirrored_ref,
)
from docker_devkit.config_lock import (
    LOCK_PATH,
    SHA256_DIGEST_REGEX,
    ImageLock,
    ImageLockEntry,
)

DIGEST_TAIL_PATTERN = re.compile(rf"@{SHA256_DIGEST_REGEX}$")
DIGEST_PATTERN = re.compile(r"^Digest:\s+(sha256:[a-f0-9]+)", re.MULTILINE)

PLATFORM_VARIABLE = "PLATFORM"


class UpstreamManifests(NamedTuple):
    index_digest: str
    platforms: dict[str, str]


def run_lock(*, upgrade: bool = False, check: bool = False) -> None:
    config = DockerDevkitConfig.load(Path.cwd())
    if config is None:
        print("no docker-devkit.yaml; no lock declarations to relock")
        return
    declared_charts = config.declared_charts
    if not config.base_images and not declared_charts:
        print("no lock declarations (neither base_images nor k8s.charts); nothing to relock")
        return

    if not check:
        write_lock(config, upgrade=upgrade)
        return
    computed = render_lock(relock(config, upgrade=upgrade))
    assert config.root is not None
    lock_file = config.root / LOCK_PATH
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
        raise SystemExit("image lock is stale")


def write_lock(config: DockerDevkitConfig, *, upgrade: bool = False) -> ImageLock:
    lock = relock(config, upgrade=upgrade)
    assert config.root is not None
    (config.root / LOCK_PATH).write_text(render_lock(lock), encoding="utf-8")
    return lock


def relock(config: DockerDevkitConfig, *, upgrade: bool = False) -> ImageLock:
    assert config.root is not None
    prefix = config.mirror_prefix
    committed = config.image_lock()

    images: dict[str, ImageLockEntry] = {}
    for name, reference in config.base_images.items():
        base = mirrored_ref(reference, prefix)
        if DIGEST_TAIL_PATTERN.search(reference):
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
    declared_charts = config.declared_charts
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
    # Render names and platform keys in sorted order.
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


def resolve_remote_manifests(reference: str, platforms: list[str], subject: str) -> UpstreamManifests:
    print(f"Resolving digests for: {reference}")
    raw = bash_output(f"docker buildx imagetools inspect --raw {shlex.quote(reference)}")
    document: object = json.loads(raw)
    index_digest = resolve_remote_digest(reference)
    children = document.get("manifests") if is_mapping(document) else None
    if not is_list(children):
        # Record a manifest answer as the index and every platform entry.
        return UpstreamManifests(index_digest, dict.fromkeys(platforms, index_digest))
    selected: dict[str, str] = {}
    for platform in platforms:
        os_name, _, architecture = platform.partition("/")
        matches = [
            child
            for child in children
            if not (
                is_mapping(child)
                and is_mapping(annotations := child.get("annotations"))
                and annotations.get("vnd.docker.reference.type") == "attestation-manifest"
            )
            if _concrete_platform(child) == (os_name, architecture)
        ]
        if len(matches) > 1:
            digests = ", ".join(_child_digest(child, subject) for child in matches)
            raise RuntimeError(f"{subject}: platform {platform} matches multiple upstream manifests ({digests})")
        if not matches:
            available = sorted({
                f"{os_name}/{architecture}"
                for child in children
                if (concrete := _concrete_platform(child)) is not None
                for os_name, architecture in (concrete,)
            })
            raise RuntimeError(
                f"{subject}: the upstream index carries no {platform} manifest "
                f"(declared platforms: {sorted(platforms)}; index platforms: {available})"
            )
        selected[platform] = _child_digest(matches[0], subject)
    return UpstreamManifests(index_digest, selected)


def resolve_remote_digest(reference: str) -> str:
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"docker buildx imagetools inspect {shlex.quote(reference)}")
    match = DIGEST_PATTERN.search(output)
    if match is None:
        raise RuntimeError(f"imagetools output for {reference} must carry a Digest line")
    return match.group(1)


def _concrete_platform(child: object) -> tuple[str, str] | None:
    if not is_mapping(child):
        return None
    platform = child.get("platform")
    if not is_mapping(platform):
        return None
    os_name = platform.get("os")
    architecture = platform.get("architecture")
    if not isinstance(os_name, str) or not isinstance(architecture, str):
        return None
    if os_name == "unknown" or architecture == "unknown":
        return None
    return (os_name, architecture)


def _child_digest(child: object, subject: str) -> str:
    if not is_mapping(child):
        raise RuntimeError(f"{subject}: upstream index child {child!r} must be a manifest descriptor")
    digest = child.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError(f"{subject}: upstream index child must carry a digest ({child!r})")
    return digest


def compose_env_entries(lock: ImageLock, config: DockerDevkitConfig, environment: Mapping[str, str]) -> dict[str, str]:
    selected = environment.get(PLATFORM_VARIABLE, "").strip()
    if selected:
        if selected not in config.platforms:
            raise RuntimeError(
                f"{PLATFORM_VARIABLE}={selected!r} must be one of the declared platforms {config.platforms}"
            )
        platform: str | None = selected
    elif not config.platforms:
        platform = None
    elif len(config.platforms) == 1:
        platform = config.platforms[0]
    else:
        raise RuntimeError(f"platforms {config.platforms} are declared; {PLATFORM_VARIABLE}= must be named in .env")
    if platform is None and lock.images:
        raise RuntimeError(
            f"{LOCK_PATH.as_posix()}: records image entries but docker-devkit.yaml declares no platforms"
        )
    return env_entries(lock, platform)


def env_entries(lock: ImageLock, platform: str | None = None) -> dict[str, str]:
    images: dict[str, str] = {}
    for name, entry in lock.images.items():
        if platform is None:
            images[name] = entry.index
            continue
        recorded = entry.platforms.get(platform)
        if recorded is None:
            raise RuntimeError(
                f"{LOCK_PATH.as_posix()}: image entry {name} records no {platform} platform manifest; "
                f"recorded platforms: {sorted(entry.platforms) or 'none'}"
            )
        images[name] = recorded
    return {**images, **lock.charts}


def render_env_entries(entries: Mapping[str, str]) -> str:
    return "".join(f"{name}={reference}\n" for name, reference in sorted(entries.items()))


def _declaration_frozen(entry: ImageLockEntry | None, base: str, platforms: list[str]) -> bool:
    if entry is None:
        return False
    return entry.index.startswith(base + "@") and sorted(entry.platforms) == sorted(platforms)
