from __future__ import annotations

from os import environ
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from boatyard.config import Config, Yaml, is_mapping

ENV_FILE = Path(".env")
BUNDLES_VARIABLE = "BUNDLES"
BUNDLE_OVERRIDE_PREFIX = "BUNDLE_OVERRIDE_"
PLATFORM_VARIABLE = "PLATFORM"
PUBLIC_URL_VARIABLE = "PUBLIC_URL"

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
            raise ValueError(f"PUBLIC_URL={value!r} must carry a scheme (https://example.dev)")
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


class Environment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundles: list[str]
    path_overrides: dict[str, Path]
    public_url: PublicUrl | None
    platform: str | None
    variables: dict[str, str]

    @model_validator(mode="before")
    @classmethod
    def _carve_overrides(cls, data: dict[str, object]) -> dict[str, object]:
        raw_overrides = data.pop("overrides", {})
        overrides = {key: str(value) for key, value in raw_overrides.items()} if is_mapping(raw_overrides) else {}
        path_overrides: dict[str, Path] = {}
        for name, value in overrides.items():
            variable = BUNDLE_OVERRIDE_PREFIX + name.upper().replace("-", "_")
            stripped = value.strip()
            if not stripped:
                raise ValueError(f"{variable}: empty; a value is a filesystem path")
            if "://" in stripped:
                raise ValueError(f"{variable}: {stripped!r} carries a URL scheme; a value is a filesystem path")
            path_overrides[name] = Path(stripped)
        data["path_overrides"] = path_overrides
        return data

    @field_validator("path_overrides")
    @classmethod
    def _require_output_layout(cls, path_overrides: dict[str, Path]) -> dict[str, Path]:
        for name, path in path_overrides.items():
            if path.parts[-4:-1] != OVERRIDE_LAYOUT:
                variable = BUNDLE_OVERRIDE_PREFIX + name.upper().replace("-", "_")
                raise ValueError(
                    f"{variable}: {path.as_posix()!r} must point into a stack/generated/compose/ bundle output layout"
                )
        return path_overrides

    @classmethod
    def load(cls, root: Path, config: Config | None) -> Environment:
        merged: dict[str, str] = {}
        path = root / ENV_FILE
        if path.is_file():
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                merged[key.strip()] = value.strip().strip("'\"")
        merged.update(environ)
        consumed = {BUNDLES_VARIABLE, PLATFORM_VARIABLE, PUBLIC_URL_VARIABLE}
        environment = cls.model_validate({
            "bundles": _split_names(merged.get(BUNDLES_VARIABLE, "")),
            "overrides": {
                key.removeprefix(BUNDLE_OVERRIDE_PREFIX).lower().replace("_", "-"): value
                for key, value in merged.items()
                if key.startswith(BUNDLE_OVERRIDE_PREFIX)
            },
            "public_url": merged.get(PUBLIC_URL_VARIABLE, "").strip() or None,
            "platform": merged.get(PLATFORM_VARIABLE, "").strip() or None,
            "variables": {
                key: value
                for key, value in merged.items()
                if key not in consumed and not key.startswith(BUNDLE_OVERRIDE_PREFIX)
            },
        })
        _refuse_malformed_selection(environment, config)
        return environment


def _split_names(value: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def _refuse_malformed_selection(environment: Environment, config: Config | None) -> None:
    if config is None or not config.services:
        if environment.bundles:
            raise ValueError(f"{BUNDLES_VARIABLE}= is set but this repo declares no services")
        return
    unknown = [name for name in environment.bundles if name not in config.selectable_options]
    if unknown:
        bare_entries_note = f"; bare entries, always included: {config.bare_entries}" if config.bare_entries else ""
        raise ValueError(
            f"{BUNDLES_VARIABLE}= names {unknown}; "
            f"the selectable options are {sorted(config.selectable_options)}{bare_entries_note}"
        )
    unknown_overrides = sorted(
        name
        for name in environment.path_overrides
        if name not in config.selectable_options and name not in config.bare_entries
    )
    if unknown_overrides:
        raise ValueError(
            f"{BUNDLE_OVERRIDE_PREFIX}* names {unknown_overrides}; "
            f"the bundle vocabulary is {sorted(config.selectable_options.keys() | set(config.bare_entries))}"
        )
    remote_names = {name for name, _reference in config.remote_service_entries()}
    local_names = sorted(name for name in environment.path_overrides if name not in remote_names)
    if local_names:
        raise ValueError(
            f"{BUNDLE_OVERRIDE_PREFIX}* names {local_names}; an override targets a ref entry; "
            f"the ref entries are {sorted(remote_names)}"
        )
