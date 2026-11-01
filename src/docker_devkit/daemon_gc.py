from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Self

from bashrun.bash import bash_check, bash_output
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

MIN_KEEP_STORAGE_GB = 60

_KEEP_STORAGE_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([TGMK]i?B)?$", re.IGNORECASE)

_GB_MULTIPLIERS = {"T": 1024, "G": 1, "M": 1 / 1024, "K": 1 / 1024**2, "B": 1 / 1024**3}


class _DaemonGc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # None is absent-or-null (the missing law refuses it); a set value measures, zero
    # included — an explicit zero is too small, not missing.
    gigabytes: float | None = Field(default=None, alias="defaultKeepStorage")

    @field_validator("gigabytes", mode="before")
    @classmethod
    def _measure(cls, value: object) -> object:
        if value is None:
            return None
        match = _KEEP_STORAGE_PATTERN.match(str(value))
        if match is None:
            raise ValueError(f"Could not parse 'defaultKeepStorage' value: {value}")
        unit = (match.group(2) or "B")[0].upper()
        return float(match.group(1)) * _GB_MULTIPLIERS[unit]


class _DaemonBuilder(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gc: _DaemonGc = Field(default_factory=_DaemonGc)


class _DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    builder: _DaemonBuilder = Field(default_factory=_DaemonBuilder)

    @model_validator(mode="after")
    def _require_keep_storage(self, info: ValidationInfo) -> Self:
        if self.builder.gc.gigabytes is not None:
            return self
        context: dict[str, object] = info.context or {}
        sample = json.dumps({"builder": {"gc": {"defaultKeepStorage": f"{MIN_KEEP_STORAGE_GB}GB"}}}, indent=2)
        raise ValueError(
            f"builder.gc.defaultKeepStorage missing in {context.get('path', 'daemon.json')}; "
            f"need >= {MIN_KEEP_STORAGE_GB}GB:\n\n{sample}"
        )


def check_gc_limits(candidates: list[Path] | None = None) -> None:
    # candidates injects the daemon.json paths for tests; default discovery probes the
    # system file, the user file, then the Windows-side home under WSL — first readable wins.
    if candidates is None:
        candidates = [Path("/etc/docker/daemon.json"), Path(os.path.expanduser("~/.docker/daemon.json"))]
        wslpath = 'wslpath $(cmd.exe /c "echo %UserProfile%" 2>/dev/null)'
        if os.getenv("WSL_DISTRO_NAME") is not None and bash_check(wslpath):
            win_home = bash_output(wslpath).strip()
            candidates.append(Path(win_home) / ".docker" / "daemon.json")

    config = next((path for path in candidates if path.exists() and os.access(path, os.R_OK)), None)
    if config is None:
        print(f"    [WARN] No readable daemon.json found. Ensure defaultKeepStorage > {MIN_KEEP_STORAGE_GB}GB.")
        return

    daemon = _DaemonConfig.model_validate(json.loads(config.read_text(encoding="utf-8")), context={"path": config})
    gigabytes = daemon.builder.gc.gigabytes
    assert gigabytes is not None  # _require_keep_storage refused absent-or-null values
    if gigabytes < MIN_KEEP_STORAGE_GB:
        raise RuntimeError(
            f"Docker GC limit {gigabytes:.1f}GB in {config} is below the required {MIN_KEEP_STORAGE_GB}GB"
        )

    print(f"    [OK] Docker GC Limit verified: {gigabytes:.1f}GB")
