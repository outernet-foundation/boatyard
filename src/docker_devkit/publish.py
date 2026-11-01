from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from docker_devkit.documents import ComposeDocument
from docker_devkit.lock import resolve_remote_digest

VAR_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(:[?\-+][^}]*)?\}")
STUB_VALUE = "publish-stub"


class PublishEntry(NamedTuple):
    artifact: str
    files: list[str]


class PublishedEntry(NamedTuple):
    artifact: str
    sha_reference: str
    branch_reference: str
    digest: str


def bake_sources(baked_directory: Path, source_names: list[str], substitutions: dict[str, str]) -> list[Path]:
    print("Bake compose YAML")
    baked_paths: list[Path] = []
    for source_name in source_names:
        source_path = Path(source_name)
        baked_text = VAR_PATTERN.sub(
            lambda match: substitutions.get(match.group(1), match.group(0)),
            source_path.read_text(encoding="utf-8"),
        )
        baked_path = baked_directory / source_path
        baked_path.parent.mkdir(parents=True, exist_ok=True)
        baked_path.write_text(baked_text, encoding="utf-8")
        baked_paths.append(baked_path)
    return baked_paths


def finalize_baked(baked_paths: list[Path], repository: str) -> None:
    print("Inline configs content")
    # The published artifact is YAML-only: file-sourced configs dangle in the include consumer's
    # materialization cache, so they ride as inline content.
    own_image_pattern = re.compile(rf"{re.escape(repository)}/[a-z0-9][a-z0-9-]*:[^\s\"'@$]+")
    digests: dict[str, str] = {}
    for baked_path in baked_paths:
        document = yaml.safe_load(baked_path.read_text(encoding="utf-8"))
        parsed = ComposeDocument.model_validate(document)
        if parsed.configs is not None:
            inlined: dict[str, dict[str, Any]] = {}
            for config_name, compose_config in parsed.configs.items():
                if compose_config.file is None:
                    # Authored content rides through verbatim: single-$ holes are the consumer's
                    # include-time interpolation surface ($ doubling is the file-sourced path only).
                    inlined[config_name] = document["configs"][config_name]
                    continue
                entry = compose_config.model_dump(exclude_defaults=True)
                entry.pop("file", None)
                # Literal $ doubles so consumer interpolation leaves env expansion to the container
                entry["content"] = Path(compose_config.file).read_text(encoding="utf-8").replace("$", "$$")
                inlined[config_name] = entry
            document["configs"] = inlined
            baked_path.write_text(
                yaml.dump(document, default_flow_style=False, sort_keys=False, width=1_000_000),
                encoding="utf-8",
            )

        # Refs under the bundle repository are this repo's own built images; publish them digest-pinned.
        text = baked_path.read_text(encoding="utf-8")
        parts: list[str] = []
        cursor = 0
        for match in own_image_pattern.finditer(text):
            parts.append(text[cursor : match.start()])
            reference = match.group(0)
            if reference not in digests:
                digests[reference] = resolve_remote_digest(reference)
            parts.append(f"{reference.rsplit(':', 1)[0]}@{digests[reference]}")
            cursor = match.end()
        parts.append(text[cursor:])
        baked_path.write_text("".join(parts), encoding="utf-8")
