from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
import yaml

from boatyard.yaml import Yaml, is_list, is_mapping

PROBE_ADAPTER_ANNOTATION = "outernet.internal/probe-adapter"
PROBE_ADAPTERS = ("python-urllib", "bash-http")
RESTART_ANNOTATION = "outernet.internal/restart"
COMPOSE_RESTART_POLICIES = ("no", "always", "on-failure", "unless-stopped")
CONSUMER_INTERPOLATED_ANNOTATION = "outernet.internal/consumer-interpolated"


def _default_metadata() -> WorkloadMetadata:
    return WorkloadMetadata()


class WorkloadDocument(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    metadata: WorkloadMetadata = Field(default_factory=_default_metadata)
    containers: dict[str, WorkloadContainer] = Field(default_factory=dict)
    resources: dict[str, WorkloadResource] = Field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Yaml, file_name: str) -> Self:
        return cls.model_validate(raw, context={"source": file_name})

    @model_validator(mode="after")
    def _require_name_matching_file_stem(self, info: ValidationInfo) -> Self:
        source = info.context.get("source") if info.context is not None else None
        if source is not None and self.metadata.name != Path(source).stem:
            raise ValueError(f"metadata.name must be {Path(source).stem!r} to match the workload file name")
        return self

    @model_validator(mode="after")
    def _validate_deployment_surface_annotations(self) -> Self:
        annotations = self.metadata.annotations

        adapter = annotations.get(PROBE_ADAPTER_ANNOTATION)
        if adapter is not None and adapter not in PROBE_ADAPTERS:
            raise ValueError(
                f"{PROBE_ADAPTER_ANNOTATION} must be one of the supported adapters {list(PROBE_ADAPTERS)}, not {adapter!r}"
            )

        restart = annotations.get(RESTART_ANNOTATION)
        if restart is not None and restart not in COMPOSE_RESTART_POLICIES:
            raise ValueError(
                f"{RESTART_ANNOTATION} must be one of the compose restart policies "
                f"{list(COMPOSE_RESTART_POLICIES)}, not {restart!r}"
            )

        consumer_interpolated = annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
        if consumer_interpolated is not None:
            if not consumer_interpolated.strip():
                raise ValueError(
                    f"{CONSUMER_INTERPOLATED_ANNOTATION} must be a non-empty comma-separated "
                    "list of the workload's files: paths"
                )
            declared_files = {target for container in self.containers.values() for target in container.files}
            for entry in consumer_interpolated.split(","):
                if entry != entry.strip() or not entry:
                    raise ValueError(
                        f"{CONSUMER_INTERPOLATED_ANNOTATION} entry {entry!r} must be non-empty and unpadded"
                    )
                if entry not in declared_files:
                    raise ValueError(
                        f"{CONSUMER_INTERPOLATED_ANNOTATION} entry {entry!r} names no declared files: path"
                    )

        return self

    @property
    def consumer_interpolated_targets(self) -> list[str]:
        annotation = self.metadata.annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
        return annotation.split(",") if annotation is not None else []


class ComposeWorkloadDocument(WorkloadDocument):
    @model_validator(mode="after")
    def _require_probe_adapter_for_http_get(self) -> Self:
        if self.metadata.annotations.get(PROBE_ADAPTER_ANNOTATION) is not None:
            return self
        for container in self.containers.values():
            for probe, block in (
                ("readinessProbe", container.readiness_probe),
                ("livenessProbe", container.liveness_probe),
            ):
                if block is not None and block.http_get is not None:
                    raise ValueError(f"a {probe} httpGet requires {PROBE_ADAPTER_ANNOTATION}")
        return self


class WorkloadMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    annotations: dict[str, str] = Field(default_factory=dict)


class WorkloadContainer(BaseModel):
    model_config = ConfigDict(extra="allow")

    image: str | None = None
    files: dict[str, Yaml] = Field(default_factory=dict)
    readiness_probe: WorkloadProbe | None = Field(default=None, alias="readinessProbe")
    liveness_probe: WorkloadProbe | None = Field(default=None, alias="livenessProbe")

    @field_validator("image")
    @classmethod
    def _require_image_slot(cls, value: str | None) -> str | None:
        if value is not None and "${" not in value and "{{" not in value:
            raise ValueError(f"image {value!r} must bind to the lock or a service sha via a ${{NAME}} slot")
        return value


class WorkloadResource(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


class WorkloadProbe(BaseModel):
    model_config = ConfigDict(extra="allow")

    http_get: Yaml | None = Field(default=None, alias="httpGet")


class LoadedWorkload(NamedTuple):
    file_name: str
    raw: Yaml
    document: WorkloadDocument


def load_workloads(score_dir: Path, names: list[str], lens: type[WorkloadDocument]) -> list[LoadedWorkload]:
    loaded: list[LoadedWorkload] = []
    for name in names:
        raw = yaml.safe_load((score_dir / name).read_text(encoding="utf-8"))
        loaded.append(LoadedWorkload(name, raw, lens.parse(raw, name)))
    return loaded


def select_provisioner_files(
    score_dir: Path,
    rendering: str,
    workloads: list[LoadedWorkload],
) -> list[tuple[str, list[Yaml]]]:
    # A provisioner file participates when an entry workload references its resource type.
    referenced = {resource.type for workload in workloads for resource in workload.document.resources.values()}
    rendering_dir = score_dir / rendering
    if not rendering_dir.is_dir():
        return []
    selected: list[tuple[str, list[Yaml]]] = []
    for path in sorted(rendering_dir.glob("*.yaml")):
        entries = yaml.safe_load(path.read_text(encoding="utf-8"))
        if is_list(entries) and any(is_mapping(entry) and entry.get("type") in referenced for entry in entries):
            selected.append((f"{rendering}/{path.name}", entries))
    return selected
