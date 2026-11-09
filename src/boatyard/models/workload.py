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
DOCKERFILE_ANNOTATION = "outernet.internal/dockerfile"
LOCK_ANNOTATION = "outernet.internal/lock"


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

        dockerfiles = _parse_source_annotation(annotations, DOCKERFILE_ANNOTATION)
        locks = _parse_source_annotation(annotations, LOCK_ANNOTATION)
        both = sorted(dockerfiles.keys() & locks.keys())
        if both:
            raise ValueError(
                f"the container {both[0]!r} is named by both {DOCKERFILE_ANNOTATION} and {LOCK_ANNOTATION}"
            )
        unsourced = sorted(set(self.containers) - dockerfiles.keys() - locks.keys())
        if unsourced:
            raise ValueError(
                f"the container {unsourced[0]!r} carries no source annotation "
                f"({DOCKERFILE_ANNOTATION} or {LOCK_ANNOTATION})"
            )
        unknown = sorted((dockerfiles.keys() | locks.keys()) - set(self.containers))
        if unknown:
            raise ValueError(f"the source annotations name no container {unknown[0]!r} of this workload")

        return self

    @property
    def consumer_interpolated_targets(self) -> list[str]:
        annotation = self.metadata.annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
        return annotation.split(",") if annotation is not None else []

    def container_sources(self) -> dict[str, tuple[str, str]]:
        dockerfiles = _parse_source_annotation(self.metadata.annotations, DOCKERFILE_ANNOTATION)
        locks = _parse_source_annotation(self.metadata.annotations, LOCK_ANNOTATION)
        return {
            **{name: ("dockerfile", value) for name, value in dockerfiles.items()},
            **{name: ("lock", value) for name, value in locks.items()},
        }


def _parse_source_annotation(annotations: dict[str, str], name: str) -> dict[str, str]:
    value = annotations.get(name)
    if value is None:
        return {}
    if not value.strip():
        raise ValueError(f"{name} must be a non-empty comma-separated list of container=source pairs")
    sources: dict[str, str] = {}
    for entry in value.split(","):
        container, separator, source = entry.partition("=")
        padded = container != container.strip() or source != source.strip()
        if not separator or padded or not container or not source:
            raise ValueError(f"{name} entry {entry!r} must be an unpadded container=source pair")
        if container in sources:
            raise ValueError(f"{name} names the container {container!r} twice")
        sources[container] = source
    return sources


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
    def _refuse_authored_image(cls, value: str | None) -> str | None:
        if value is not None:
            raise ValueError(
                f"image is never authored in a workload file; declare {DOCKERFILE_ANNOTATION} "
                f"or {LOCK_ANNOTATION} instead"
            )
        return value


class WorkloadResource(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


class WorkloadProbe(BaseModel):
    model_config = ConfigDict(extra="allow")

    http_get: Yaml | None = Field(default=None, alias="httpGet")


class LoadedWorkload(NamedTuple):
    file_name: str
    text: str
    raw: Yaml
    document: WorkloadDocument


def load_workload(score_dir: Path, file_name: str, lens: type[WorkloadDocument]) -> LoadedWorkload:
    workload_path = score_dir / file_name
    if not workload_path.is_file():
        raise ValueError(f"{workload_path.as_posix()}: the workload file is required")
    text = workload_path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    return LoadedWorkload(file_name, text, raw, lens.parse(raw, file_name))


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
