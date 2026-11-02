from __future__ import annotations

import re
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

# The lock pairs with the root config at the repo root: build keys declared
# (base_images or charts) => the committed lock is required.
LOCK_PATH = Path("docker-devkit.lock")

PLATFORM_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")

SHA256_DIGEST_REGEX = r"sha256:[a-f0-9]{64}"


class ImageLock(BaseModel):
    # The structured lock beside the root config: registry-resolved facts, written by the
    # lock verb and build's incremental freeze, never hand-authored.
    model_config = ConfigDict(extra="forbid")

    images: dict[str, ImageLockEntry] = Field(default_factory=dict)
    charts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        for name, reference in self.charts.items():
            if not reference.rpartition("@")[2].startswith("sha256:"):
                raise ValueError(
                    f"{context.get('path', 'the image lock')}: chart {name} is not digest-pinned ({reference!r})"
                )
        shared = sorted(self.images.keys() & self.charts.keys())
        if shared:
            raise ValueError(
                f"{context.get('path', 'the image lock')}: names {shared} appear under both images and charts; "
                "the env projection carries one flat vocabulary"
            )
        return self

    def require(self, name: str, subject: str, hint: str = "the lock verb writes it") -> str:
        # The one "lock lacks NAME" refusal every lock consumer raises; the image entry
        # answers by its index reference, the chart entry flat.
        if name in self.images:
            return self.images[name].index
        if name in self.charts:
            return self.charts[name]
        raise RuntimeError(f"{subject}: {LOCK_PATH.as_posix()} lacks the {name} entry ({hint})")


class ImageLockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: str
    platforms: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_digest_pins(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        source = context.get("path", "the image lock")
        for site, reference in [("index", self.index), *self.platforms.items()]:
            if not reference.rpartition("@")[2].startswith("sha256:"):
                raise ValueError(f"{source}: entry field {site} is not digest-pinned ({reference!r})")
        for platform in self.platforms:
            if PLATFORM_PATTERN.fullmatch(platform) is None:
                raise ValueError(f"{source}: platform key {platform!r} must be an os/arch pair like linux/arm64")
        return self

    @property
    def recorded_digests(self) -> list[str]:
        # The digest set the up/mirror presence probes walk: the index plus every platform manifest.
        return [self.index, *self.platforms.values()]


def parse_image_lock(path: Path) -> ImageLock:
    text = path.read_text(encoding="utf-8")
    source = path.as_posix()
    try:
        document_node = yaml.SafeLoader(text).get_single_node()
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f"{source}:{mark.line + 1}" if mark is not None else source
        raise ValueError(f"{location}: {error}") from error
    if document_node is not None:
        _reject_duplicate_keys(document_node, source)
    loaded: object = yaml.safe_load(text)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise TypeError(
            f"{source}: the lock is a YAML mapping of images:/charts: sections, "
            f"not a bare {type(loaded).__name__}"
        )
    return ImageLock.model_validate(loaded, context={"path": source})


def _reject_duplicate_keys(node: yaml.Node, source: str) -> None:
    # The stock loader silently keeps the last of duplicate mapping keys; the lock
    # refuses them, with the offending line from the node mark.
    if isinstance(node, yaml.MappingNode):
        seen: set[object] = set()
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, yaml.ScalarNode) else repr(key_node.value)
            if key in seen:
                raise ValueError(f"{source}: line {key_node.start_mark.line + 1}: duplicate key {key!r}")
            seen.add(key)
            _reject_duplicate_keys(value_node, source)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _reject_duplicate_keys(item, source)
