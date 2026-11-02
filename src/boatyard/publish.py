from __future__ import annotations

import re
from functools import partial
from pathlib import Path
from typing import NamedTuple

import yaml

from boatyard.config import Yaml, double_dollars
from boatyard.score_compose import ComposeDocument
from boatyard.lock import resolve_remote_digest

VAR_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(:[?\-+][^}]*)?\}")
STUB_VALUE = "publish-stub"


class PublishedEntry(NamedTuple):
    artifact: str
    sha_reference: str
    branch_reference: str
    digest: str


def bake_sources(baked_directory: Path, source_name: str, substitutions: dict[str, str]) -> Path:
    print("Bake compose YAML")
    source_path = Path(source_name)
    baked_text = VAR_PATTERN.sub(
        lambda match: substitutions.get(match.group(1), match.group(0)),
        source_path.read_text(encoding="utf-8"),
    )
    baked_path = baked_directory / source_path
    baked_path.parent.mkdir(parents=True, exist_ok=True)
    baked_path.write_text(baked_text, encoding="utf-8")
    return baked_path


def finalize_baked(baked_path: Path, repository: str) -> None:
    print("Inline configs content")
    # Inline file-sourced configs as content entries.
    own_image_pattern = re.compile(rf"{re.escape(repository)}/[a-z0-9][a-z0-9-]*:[^\s\"'@$]+")
    document = yaml.safe_load(baked_path.read_text(encoding="utf-8"))
    parsed = ComposeDocument.model_validate(document)
    if parsed.configs is not None:
        inlined: dict[str, dict[str, Yaml]] = {}
        for config_name, compose_config in parsed.configs.items():
            if compose_config.file is None:
                inlined[config_name] = document["configs"][config_name]
                continue
            entry = compose_config.model_dump(exclude_defaults=True)
            entry.pop("file", None)
            entry["content"] = double_dollars(Path(compose_config.file).read_text(encoding="utf-8"))
            inlined[config_name] = entry
        document["configs"] = inlined
        baked_path.write_text(
            yaml.dump(document, default_flow_style=False, sort_keys=False, width=1_000_000),
            encoding="utf-8",
        )

    # Pin image refs under the repository to digests.
    digests: dict[str, str] = {}
    baked_path.write_text(
        own_image_pattern.sub(partial(_pinned_reference, digests), baked_path.read_text(encoding="utf-8")),
        encoding="utf-8",
    )


def _pinned_reference(digests: dict[str, str], match: re.Match[str]) -> str:
    reference = match.group(0)
    if reference not in digests:
        digests[reference] = resolve_remote_digest(reference)
    return f"{reference.rsplit(':', 1)[0]}@{digests[reference]}"
