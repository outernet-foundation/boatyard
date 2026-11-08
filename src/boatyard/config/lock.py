from __future__ import annotations

import re
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from boatyard.config.config import PLATFORM_KEY, ConfigFile, SlotDemand, bundle_lock_key, chart_lock_key
from boatyard.paths import LOCK_PATH, VALUES_FILE
from boatyard.temp import PLATFORM_PATTERN, SHA256_DIGEST_REGEX
from boatyard.yaml import load_yaml_model


class LockFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platforms: list[str] = Field(default_factory=list)
    images: dict[str, ConfigLockEntry] = Field(default_factory=dict)
    charts: dict[str, str] = Field(default_factory=dict)
    bundles: dict[str, ConfigLockBundle] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        source = str(context.get("path", "the image lock"))
        for name, reference in self.charts.items():
            if not _digest_pinned(reference):
                raise ValueError(f"{source}: chart {name} is not digest-pinned ({reference!r})")
        shared = sorted(
            (self.images.keys() & self.charts.keys())
            | (self.images.keys() & self.bundles.keys())
            | (self.charts.keys() & self.bundles.keys())
        )
        if shared:
            raise ValueError(f"{source}: names {shared} must appear under at most one of images, charts, bundles")
        config = context.get("config")
        if isinstance(config, ConfigFile):
            self._require_paired(config, source)
        return self

    def _require_paired(self, config: ConfigFile, source: str) -> None:
        recorded = sorted(self.platforms)
        declared = sorted(config.platforms)
        if recorded != declared:
            raise ValueError(
                f"{source}: records platforms {recorded} but boatyard.yml declares {declared} (run boatyard lock)"
            )
        for name, entry in self.images.items():
            if sorted(entry.platforms) != recorded:
                raise ValueError(
                    f"{source}: image entry {name} records platforms {sorted(entry.platforms)} "
                    f"but the lock records {recorded} (run boatyard lock)"
                )
        missing_images = sorted(set(config.base_images) - self.images.keys())
        if missing_images:
            raise ValueError(f"{source}: lacks the {missing_images} entries base_images declares (run boatyard lock)")
        missing_charts = sorted({chart_lock_key(chart) for chart in config.declared_charts} - self.charts.keys())
        if missing_charts:
            raise ValueError(f"{source}: lacks the {missing_charts} entries k8s.charts declares (run boatyard lock)")
        declared_names = (
            set(config.base_images)
            | {chart_lock_key(chart) for chart in config.declared_charts}
            | {bundle_lock_key(name) for name, _reference, _entry in config.remote_declared_entries()}
        )
        orphans = sorted((self.images.keys() | self.charts.keys() | self.bundles.keys()) - declared_names)
        if orphans:
            raise ValueError(
                f"{source}: records entries boatyard.yml no longer declares ({orphans}); run boatyard lock"
            )

    @classmethod
    def load(cls, path: Path, config: ConfigFile) -> LockFile:
        context: dict[str, object] = {"path": path.as_posix(), "config": config}
        if not path.is_file():
            # Every consumer reads a missing lock as the empty lock paired against the config.
            return cls.model_validate({}, context=context)
        return load_yaml_model(cls, path, context)

    def require(self, name: str, subject: str) -> str:
        if name in self.images:
            return self.images[name].index
        if name in self.charts:
            return self.charts[name]
        if name in self.bundles:
            return self.bundles[name].reference
        raise ValueError(f"{subject}: {LOCK_PATH.as_posix()} lacks the {name} entry (run boatyard lock)")

    def index_references(self) -> dict[str, str]:
        return {**{name: entry.index for name, entry in self.images.items()}, **self.charts}

    def manifest_references(self, platform: str | None) -> dict[str, str]:
        resolved: str | None
        if platform is None:
            if not self.platforms:
                resolved = None
            elif len(self.platforms) == 1:
                resolved = self.platforms[0]
            else:
                raise ValueError(
                    f"platforms {self.platforms} are recorded; {PLATFORM_KEY}: must be named in {VALUES_FILE.name}"
                )
        elif self.platforms and platform not in self.platforms:
            raise ValueError(f"{PLATFORM_KEY}: {platform!r} must be one of the recorded platforms {self.platforms}")
        else:
            resolved = platform
        if resolved is None:
            return self.index_references()
        return {**{name: entry.platforms[resolved] for name, entry in self.images.items()}, **self.charts}


class ConfigLockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: str
    platforms: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_digest_pins(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        source = str(context.get("path", "the image lock"))
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
    slots: dict[str, SlotDemand] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_digest_pin(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        source = str(context.get("path", "the image lock"))
        if not _digest_pinned(self.reference):
            raise ValueError(f"{source}: bundle entry is not digest-pinned ({self.reference!r})")
        return self


def _digest_pinned(reference: str) -> bool:
    return re.fullmatch(SHA256_DIGEST_REGEX, reference.rpartition("@")[2]) is not None
