from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import NamedTuple

from bashrun.bash import bash_output

from docker_devkit.config import is_list, is_mapping

BUILD_ARG_PATTERN = re.compile(r"\$\{[A-Za-z0-9_]+\}")
FROM_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
COPY_FROM_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.MULTILINE)
IMAGE_LINE_PATTERN = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)", re.MULTILINE)
DIGEST_PATTERN = re.compile(r"^Digest:\s+(sha256:[a-f0-9]+)", re.MULTILINE)
ATTESTATION_TYPE = "attestation-manifest"
REFERENCE_TYPE_ANNOTATION = "vnd.docker.reference.type"


class UpstreamManifests(NamedTuple):
    index_digest: str
    platforms: dict[str, str]


def unpinned_references(root: Path) -> list[str]:
    dockerfile_globs = ("workloads/*/Dockerfile*", "docker/*/Dockerfile*")
    image_globs = ("stack/score/**/*.yaml", "score/**/*.yaml")
    dockerfile_paths = [path for dockerfile_glob in dockerfile_globs for path in sorted(root.glob(dockerfile_glob))]
    image_paths = [path for image_glob in image_globs for path in sorted(root.glob(image_glob))]
    strays = (
        [
            match.group(1)
            for path in dockerfile_paths
            for match in FROM_PATTERN.finditer(path.read_text(encoding="utf-8"))
        ]
        + [
            match.group(1)
            for path in dockerfile_paths
            for match in COPY_FROM_PATTERN.finditer(path.read_text(encoding="utf-8"))
            if "/" in match.group(1)
        ]
        + [
            match.group(1)
            for path in image_paths
            for match in IMAGE_LINE_PATTERN.finditer(path.read_text(encoding="utf-8"))
        ]
    )
    return [
        reference
        for reference in strays
        if not BUILD_ARG_PATTERN.search(reference)
        if "/" in (stripped := BUILD_ARG_PATTERN.sub("", reference).strip())
        if ":" not in (tail := stripped[stripped.rfind("/") + 1 :])
        if "@" not in tail
    ]


def resolve_remote_digest(reference: str) -> str:
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"docker buildx imagetools inspect {shlex.quote(reference)}")
    match = DIGEST_PATTERN.search(output)
    if match is None:
        raise RuntimeError(f"Could not parse digest for image reference: {reference}")
    return match.group(1)


def resolve_remote_manifests(reference: str, platforms: list[str], subject: str) -> UpstreamManifests:
    # The complete resolution against the upstream reference: the index digest plus one
    # concrete manifest digest per declared platform. Selection reads the declared
    # platforms only — the writing machine's arch never participates.
    print(f"Resolving digests for: {reference}")
    raw = bash_output(f"docker buildx imagetools inspect --raw {shlex.quote(reference)}")
    document: object = json.loads(raw)
    index_digest = resolve_remote_digest(reference)
    children = document.get("manifests") if is_mapping(document) else None
    if not is_list(children):
        # A reference that answers a manifest rather than an index has one concrete
        # byte-stream; it records uniformly as the index and every platform entry.
        return UpstreamManifests(index_digest, dict.fromkeys(platforms, index_digest))
    return UpstreamManifests(index_digest, select_platform_manifests(children, platforms, subject))


def select_platform_manifests(children: list[object], platforms: list[str], subject: str) -> dict[str, str]:
    selected: dict[str, str] = {}
    for platform in platforms:
        os_name, _, architecture = platform.partition("/")
        matches = [
            child
            for child in children
            if not _is_attestation(child)
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
    return selected


def _is_attestation(child: object) -> bool:
    if not is_mapping(child):
        return False
    annotations = child.get("annotations")
    return is_mapping(annotations) and annotations.get(REFERENCE_TYPE_ANNOTATION) == ATTESTATION_TYPE


def _concrete_platform(child: object) -> tuple[str, str] | None:
    # The (os, architecture) pair of an index child; None for entries carrying no
    # concrete platform — unknown/unknown descriptors and platform-less entries never match.
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
        raise TypeError(f"{subject}: upstream index child {child!r} is not a manifest descriptor")
    digest = child.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError(f"{subject}: upstream index child carries no digest ({child!r})")
    return digest
