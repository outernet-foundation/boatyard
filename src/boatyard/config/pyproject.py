from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

PYPROJECT_PATH = Path("pyproject.toml")


class PyprojectToml(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool: ToolTable | None = None


class ToolTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uv: UvToolTable | None = None


class UvToolTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    required_version: str | None = Field(default=None, alias="required-version")
