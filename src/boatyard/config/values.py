from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from boatyard.config.config import (
    ENV_NAME_PATTERN,
    Config,
    Yaml,
    is_mapping,
    qualified_option_name,
)
from boatyard.config.lock import load_yaml_model
from boatyard.paths import VALUES_FILE

PLATFORM_KEY = "platform"
TYPED_KEYS = frozenset({"bundles", "path_overrides", "public_url", PLATFORM_KEY})

OVERRIDE_LAYOUT = ("stack", "generated", "compose")


class PublicUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forwarded_proto: str
    forwarded_port: str

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: str) -> dict[str, str]:
        scheme, separator, rest = value.partition("://")
        if not separator:
            raise ValueError(f"public_url: {value!r} must carry a scheme (https://example.dev)")
        hostport = rest.partition("/")[0]
        if ":" in hostport:
            forwarded_port = hostport.rpartition(":")[2]
        elif scheme == "https":
            forwarded_port = "443"
        else:
            forwarded_port = "80"
        return {"forwarded_proto": scheme, "forwarded_port": forwarded_port}

    @property
    def forwarded(self) -> dict[str, Yaml]:
        return {"FORWARDED_PROTO": self.forwarded_proto, "FORWARDED_PORT": self.forwarded_port}


class ConfigValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choices: dict[str, str]
    path_overrides: dict[str, Path]
    public_url: PublicUrl | None
    platform: str | None
    variables: dict[str, str]

    @model_validator(mode="before")
    @classmethod
    def _fold_selection(cls, data: object, info: ValidationInfo) -> object:
        if not is_mapping(data):
            return data
        context: dict[str, object] = info.context or {}
        config = context.get("config")
        raw_bundles = data.get("bundles", [])
        if not isinstance(raw_bundles, list):
            raise TypeError("bundles: must be a list of selection names")
        bundles: list[str] = []
        for name in raw_bundles:
            if not isinstance(name, str):
                raise TypeError("bundles: must be a list of selection names")
            bundles.append(name)
        choices: dict[str, str] = {}
        if not isinstance(config, Config) or not config.services:
            if bundles:
                raise ValueError("bundles: is set but this repo declares no services")
        else:
            selectable = config.selectable_options
            bare = config.bare_entries
            unknown = [name for name in bundles if name not in selectable]
            if unknown:
                bare_entries_note = f"; bare entries, always included: {bare}" if bare else ""
                raise ValueError(
                    f"bundles: names {unknown}; the selectable options are {sorted(selectable)}{bare_entries_note}"
                )
            # Fold the selection into one choice per group
            for name in bundles:
                group, option_name = selectable[name]
                if group in choices:
                    raise ValueError(
                        f"bundles: selects {qualified_option_name(group, choices[group])} and {name}; "
                        f"at most one option per group ({group})"
                    )
                choices[group] = option_name
        variables: dict[str, str] = {
            str(key): (str(value).lower() if isinstance(value, bool) else str(value))
            for key, value in data.items()
            if str(key) not in TYPED_KEYS
        }
        return {
            "choices": choices,
            "path_overrides": data.get("path_overrides", {}),
            "public_url": data.get("public_url"),
            "platform": data.get("platform"),
            "variables": variables,
        }

    @model_validator(mode="after")
    def _require_ref_entry_overrides(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        config = context.get("config")
        if not isinstance(config, Config) or not config.services:
            return self
        vocabulary = set(config.selectable_options) | set(config.bare_entries)
        remote_names = {name for name, _declared, _entry in config.remote_declared_entries()}
        unknown_overrides: list[str] = []
        local_names: list[str] = []
        for name in self.path_overrides:
            if name not in vocabulary:
                unknown_overrides.append(name)
            elif name not in remote_names:
                local_names.append(name)
        if unknown_overrides:
            raise ValueError(
                f"path_overrides: names {sorted(unknown_overrides)}; the bundle vocabulary is {sorted(vocabulary)}"
            )
        if local_names:
            raise ValueError(
                f"path_overrides: names {sorted(local_names)}; an override targets a ref entry; "
                f"the ref entries are {sorted(remote_names)}"
            )
        return self

    @field_validator("path_overrides", mode="before")
    @classmethod
    def _require_path_values(cls, raw_overrides: object) -> object:
        if not is_mapping(raw_overrides):
            return raw_overrides
        path_overrides: dict[str, Path] = {}
        for name, value in raw_overrides.items():
            stripped = str(value).strip()
            if not stripped:
                raise ValueError(f"path_overrides.{name}: empty; a value is a filesystem path")
            if "://" in stripped:
                raise ValueError(
                    f"path_overrides.{name}: {stripped!r} carries a URL scheme; a value is a filesystem path"
                )
            path_overrides[name] = Path(stripped)
        return path_overrides

    @field_validator("path_overrides")
    @classmethod
    def _require_output_layout(cls, path_overrides: dict[str, Path]) -> dict[str, Path]:
        for name, path in path_overrides.items():
            if path.parts[-4:-1] != OVERRIDE_LAYOUT:
                raise ValueError(
                    f"path_overrides.{name}: {path.as_posix()!r} must point into a stack/generated/compose/ bundle output layout"
                )
        return path_overrides

    @field_validator("variables")
    @classmethod
    def _require_env_names(cls, variables: dict[str, str], info: ValidationInfo) -> dict[str, str]:
        context: dict[str, object] = info.context or {}
        source = context.get("path", VALUES_FILE.name)
        unknown = sorted(key for key in variables if ENV_NAME_PATTERN.fullmatch(key) is None)
        if unknown:
            raise ValueError(f"{source}: keys {unknown} are not ALL_CAPS environment names")
        return variables

    @classmethod
    def load(cls, path: Path, config: Config | None) -> ConfigValues:
        return load_yaml_model(cls, path, {"path": path.as_posix(), "config": config})
