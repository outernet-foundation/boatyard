from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from boatyard.config.config import (
    ENV_NAME_PATTERN,
    Config,
    Yaml,
    is_mapping,
    load_yaml_file,
    qualified_option_name,
)

VALUES_FILE = Path("boatyard.values.yaml")
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

    bundles: list[str]
    choices: dict[str, str]
    path_overrides: dict[str, Path]
    public_url: PublicUrl | None
    platform: str | None
    variables: dict[str, str]

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
                raise ValueError(f"path_overrides.{name}: {stripped!r} carries a URL scheme; a value is a filesystem path")
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
    def _require_env_names(cls, variables: dict[str, str]) -> dict[str, str]:
        unknown = sorted(key for key in variables if ENV_NAME_PATTERN.fullmatch(key) is None)
        if unknown:
            raise ValueError(f"{VALUES_FILE.name}: keys {unknown} are not ALL_CAPS environment names")
        return variables

    @classmethod
    def load(cls, root: Path, config: Config | None) -> ConfigValues:
        path = root / VALUES_FILE
        raw: Yaml = load_yaml_file(path) if path.is_file() else {}
        if raw is None:
            raw = {}
        if not is_mapping(raw):
            raise ValueError(f"{path.as_posix()}: the values file must be a YAML mapping")
        raw_values = {str(key): value for key, value in raw.items()}
        raw_bundles = raw_values.get("bundles", [])
        if not isinstance(raw_bundles, list):
            raise TypeError("bundles: must be a list of selection names")
        bundles = [name for name in raw_bundles if isinstance(name, str)]
        if len(bundles) != len(raw_bundles):
            raise TypeError("bundles: must be a list of selection names")
        choices: dict[str, str] = {}
        if config is None or not config.services:
            if bundles:
                raise ValueError("bundles: is set but this repo declares no services")
        else:
            unknown = [name for name in bundles if name not in config.selectable_options]
            if unknown:
                bare_entries_note = (
                    f"; bare entries, always included: {config.bare_entries}" if config.bare_entries else ""
                )
                raise ValueError(
                    f"bundles: names {unknown}; "
                    f"the selectable options are {sorted(config.selectable_options)}{bare_entries_note}"
                )
            # Fold the selection into one choice per group
            for name in bundles:
                group, option_name = config.selectable_options[name]
                if group in choices:
                    raise ValueError(
                        f"bundles: selects {qualified_option_name(group, choices[group])} and {name}; "
                        f"at most one option per group ({group})"
                    )
                choices[group] = option_name
        environment = cls.model_validate({
            "bundles": bundles,
            "choices": choices,
            "path_overrides": raw_values.get("path_overrides", {}),
            "public_url": raw_values.get("public_url"),
            "platform": raw_values.get("platform"),
            "variables": {
                key: (str(value).lower() if isinstance(value, bool) else str(value))
                for key, value in raw_values.items()
                if key not in TYPED_KEYS
            },
        })
        if config is None or not config.services:
            return environment
        unknown_overrides = sorted(
            name
            for name in environment.path_overrides
            if name not in config.selectable_options and name not in config.bare_entries
        )
        if unknown_overrides:
            raise ValueError(
                f"path_overrides: names {unknown_overrides}; "
                f"the bundle vocabulary is {sorted(config.selectable_options.keys() | set(config.bare_entries))}"
            )
        remote_names = {name for name, _reference in config.remote_service_entries()}
        local_names = sorted(name for name in environment.path_overrides if name not in remote_names)
        if local_names:
            raise ValueError(
                f"path_overrides: names {local_names}; an override targets a ref entry; "
                f"the ref entries are {sorted(remote_names)}"
            )
        return environment
