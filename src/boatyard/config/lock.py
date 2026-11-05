from __future__ import annotations

import re
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from boatyard.paths import LOCK_PATH
from boatyard.temp import PLATFORM_PATTERN, SHA256_DIGEST_REGEX
from boatyard.yaml import load_yaml_model


class LockFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: dict[str, ConfigLockEntry] = Field(default_factory=dict)
    charts: dict[str, str] = Field(default_factory=dict)
    bundles: dict[str, ConfigLockBundle] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        for name, reference in self.charts.items():
            if not _digest_pinned(reference):
                raise ValueError(
                    f"{context.get('path', 'the image lock')}: chart {name} is not digest-pinned ({reference!r})"
                )
        shared = sorted(
            (self.images.keys() & self.charts.keys())
            | (self.images.keys() & self.bundles.keys())
            | (self.charts.keys() & self.bundles.keys())
        )
        if shared:
            raise ValueError(
                f"{context.get('path', 'the image lock')}: names {shared} must appear under "
                "at most one of images, charts, bundles"
            )
        return self

    def require(self, name: str, subject: str, hint: str = "the lock verb writes it") -> str:
        if name in self.images:
            return self.images[name].index
        if name in self.charts:
            return self.charts[name]
        if name in self.bundles:
            return self.bundles[name].reference
        raise ValueError(f"{subject}: {LOCK_PATH.as_posix()} lacks the {name} entry ({hint})")

    @classmethod
    def load(cls, path: Path) -> LockFile:
        return load_yaml_model(cls, path, {"path": path.as_posix()})


class ConfigLockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: str
    platforms: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_digest_pins(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        source = context.get("path", "the image lock")
        for site, reference in [("index", self.index), *self.platforms.items()]:
            if not _digest_pinned(reference):
                raise ValueError(f"{source}: entry field {site} is not digest-pinned ({reference!r})")
        for platform in self.platforms:
            if PLATFORM_PATTERN.fullmatch(platform) is None:
                raise ValueError(f"{source}: platform key {platform!r} must be an os/arch pair like linux/arm64")
        return self

    @property
    def recorded_digests(self) -> list[str]:
        return [self.index, *self.platforms.values()]


class ConfigLockBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str
    requires: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_digest_pin(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        source = context.get("path", "the image lock")
        if not _digest_pinned(self.reference):
            raise ValueError(f"{source}: bundle entry is not digest-pinned ({self.reference!r})")
        return self


def _digest_pinned(reference: str) -> bool:
    return re.fullmatch(SHA256_DIGEST_REGEX, reference.rpartition("@")[2]) is not None


def read_config_lock(root: Path) -> LockFile:
    lock_path = root / LOCK_PATH
    return LockFile.load(lock_path) if lock_path.is_file() else LockFile()
