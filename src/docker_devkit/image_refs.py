from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from bashrun.bash import bash_output

BUILD_ARG_PATTERN = re.compile(r"\$\{[A-Za-z0-9_]+\}")
FROM_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)
COPY_FROM_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.MULTILINE)
IMAGE_LINE_PATTERN = re.compile(r"^\s*image:\s*[\"']?([^\s\"']+)", re.MULTILINE)
DIGEST_PATTERN = re.compile(r"^Digest:\s+(sha256:[a-f0-9]+)", re.MULTILINE)


@dataclass(frozen=True)
class ImageReference:
    name: str
    reference: str


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
