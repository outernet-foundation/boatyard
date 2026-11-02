from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from docker_devkit.config import DEV_TOGGLES_ANNOTATION


class ComposeDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    services: dict[str, ComposeService] | None = None
    configs: dict[str, ComposeConfig] | None = None

    @model_validator(mode="after")
    def _reject_colliding_bind_basenames(self) -> Self:
        # Config names are bind basenames, so two binds sharing one collide however each
        # resolves; the model sees the raw sources, and their basenames never change.
        seen: dict[str, str] = {}
        for service in (self.services or {}).values():
            for volume in service.volumes or []:
                if not isinstance(volume, BindVolume):
                    continue
                source = volume.source
                known = seen.setdefault(Path(source).name, source)
                if known != source:
                    raise ValueError(f"bind sources {known} and {source} collide on config name {Path(source).name}")
        return self


class ComposeService(BaseModel):
    model_config = ConfigDict(extra="allow")

    volumes: list[BindVolume | OtherVolume] | None = None


class BindVolume(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["bind"]
    source: str
    target: str

    @field_validator("source")
    @classmethod
    def _require_relative(cls, value: str) -> str:
        if Path(value).is_absolute():
            raise ValueError(f"absolute bind source {value} cannot be inlined hermetically")
        return value


class OtherVolume(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str

    # Reject malformed binds so they fail validation instead of passing through as non-binds.
    @model_validator(mode="after")
    def _reject_bind(self) -> Self:
        if self.type == "bind":
            raise ValueError("a bind volume is missing its source or target")
        return self


class ComposeConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str | None = None


class WorkloadMetadata(BaseModel):
    # Shape only: value laws (annotation spellings, name presence) stay at their consumers.
    model_config = ConfigDict(extra="allow")

    name: object = None
    annotations: dict[str, object] = Field(default_factory=dict)


class WorkloadDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    metadata: WorkloadMetadata = Field(default_factory=WorkloadMetadata)

    @property
    def dev_toggles(self) -> tuple[object | None, object]:
        # The raw dev-toggles annotation value (None when absent) beside the service name;
        # consumers own their refuse/skip policies.
        return self.metadata.annotations.get(DEV_TOGGLES_ANNOTATION), self.metadata.name
