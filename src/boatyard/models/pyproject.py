from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

PYPROJECT_PATH = Path("pyproject.toml")


class PyprojectToml(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool: ToolTable | None = None

    def uv_version_pin(self) -> str:
        declared = self.tool.uv.required_version if self.tool is not None and self.tool.uv is not None else None
        if not declared:
            raise ValueError(f"{PYPROJECT_PATH.as_posix()}: tool.uv.required-version is not declared")
        pin_match = re.fullmatch(r"==(?P<version>[^,*\s]+)", declared.strip())
        if pin_match is None:
            raise ValueError(
                f"{PYPROJECT_PATH.as_posix()}: tool.uv.required-version {declared!r} "
                "must be an exact == pin like '==0.12.15'"
            )
        return pin_match.group("version")


class ToolTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uv: UvToolTable | None = None


class UvToolTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    required_version: str | None = Field(default=None, alias="required-version")
