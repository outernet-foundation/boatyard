from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from boatyard.compose import merged_environment
from boatyard.config import (
    COMPOSE_HOLE,
    ComposeWorkloadDocument,
    EDGE_SERVICE_NAME,
    Yaml,
    compose_bundle_output_path,
    double_dollars,
    Config,
)
from boatyard.config_lock import LOCK_PATH, ConfigLock
from boatyard.lock import compose_env_entries
from boatyard.score import (
    SECRETS_FILE,
    SecretsDeclaration,
    dump_compose,
    render_score,
    write_generated_file,
)

STATE_PREFIX = ".score-compose/"

BARRIER_SERVICE = "wait-for-resources"
HEALTHCHECK_TEMPLATE = "probe-adapter-healthchecks.tpl"
RESTART_TEMPLATE = "restart.tpl"
GPU_COMPOSE_TEMPLATES = {"cuda": "gpu-cuda.tpl", "rocm": "gpu-rocm.tpl"}


class ComposeDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    services: dict[str, ComposeService] = Field(default_factory=dict)
    configs: dict[str, ComposeConfig] | None = None

    @model_validator(mode="after")
    def _reject_colliding_bind_basenames(self) -> Self:
        seen: dict[str, str] = {}
        for service in self.services.values():
            for volume in service.bind_volumes:
                source = volume.source
                known = seen.setdefault(Path(source).name, source)
                if known != source:
                    raise ValueError(f"bind sources {known} and {source} collide on config name {Path(source).name}")
        return self

    def dump(self) -> Yaml:
        return self.model_dump(exclude_none=True)


class ComposeService(BaseModel):
    model_config = ConfigDict(extra="allow")

    volumes: list[BindVolume | OtherVolume] | None = None
    depends_on: dict[str, Yaml] | list[str] | None = None
    configs: list[ComposeConfigReference] | None = None
    image: str | None = None

    @property
    def bind_volumes(self) -> list[BindVolume]:
        return [volume for volume in self.volumes or [] if isinstance(volume, BindVolume)]

    def strip_bind_volumes(self) -> list[BindVolume]:
        binds = self.bind_volumes
        self.volumes = [volume for volume in self.volumes or [] if not isinstance(volume, BindVolume)] or None
        return binds


class ComposeConfigReference(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: str
    target: str


class BindVolume(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["bind"]
    source: str
    target: str

    @field_validator("source")
    @classmethod
    def _require_relative(cls, value: str) -> str:
        if Path(value).is_absolute():
            raise ValueError(f"bind source {value} must be relative")
        return value


class OtherVolume(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str

    @model_validator(mode="after")
    def _reject_bind(self) -> Self:
        if self.type == "bind":
            raise ValueError("a bind volume is missing its source or target")
        return self


class ComposeConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str | None = None
    content: str | None = None


def generate_compose(
    config: Config, lock: ConfigLock, service_shas: dict[str, str], secrets: SecretsDeclaration
) -> list[Path]:
    # Collect the local bundles in declaration order
    bundles = config.local_service_entries()
    if not bundles:
        raise ValueError("the compose target requires local service entries (workloads:)")
    root = config.root
    score_dir = root / config.score_dir
    score_state = score_dir / STATE_PREFIX
    images = compose_env_entries(lock, config, merged_environment(root))
    if "ALPINE_IMAGE" not in images:
        raise ValueError(f"{LOCK_PATH.as_posix()}: the image lock must carry the ALPINE_IMAGE entry")

    outputs: list[Path] = []
    for name, entry in bundles:
        workloads = [
            (Path(workload_file).stem, ComposeWorkloadDocument.load(score_dir / workload_file))
            for workload_file in entry.workloads
        ]
        barrier = f"{BARRIER_SERVICE}-{name}"

        # Render the bundle from a clean state dir
        shutil.rmtree(score_state, ignore_errors=True)
        output_path = compose_bundle_output_path(root, name)
        render_score(
            config,
            is_k8s=False,
            workloads=entry.workloads,
            rendering_vars=entry.vars,
            templates=[
                HEALTHCHECK_TEMPLATE,
                RESTART_TEMPLATE,
                *(
                    template
                    for variant, template in GPU_COMPOSE_TEMPLATES.items()
                    if variant == entry.vars.get("GPU_VARIANT", "")
                ),
            ],
            output=output_path,
            images=images,
            service_shas=service_shas,
        )

        # Parse the rendering into the typed document
        document = ComposeDocument.model_validate(yaml.safe_load(output_path.read_text(encoding="utf-8")))

        # Attribute rendered container services to their owning workloads
        services_map: dict[str, tuple[ComposeService, ComposeWorkloadDocument | None, str]] = {
            (service_name := f"{workload_name}-{container_name}"): (
                document.services[service_name],
                workload,
                container_name,
            )
            for workload_name, workload in workloads
            for container_name in workload.containers
        }

        # Carry workload-less services through under their rendered names
        for service_name, service in document.services.items():
            if service_name in services_map:
                continue

            # Rename the barrier to the per-entry name and pin its image to the lock hole
            if service_name == BARRIER_SERVICE:
                barrier_service = document.services[BARRIER_SERVICE]
                barrier_service.image = "${ALPINE_IMAGE:?err}"
                services_map[BARRIER_SERVICE] = (barrier_service, None, barrier)
                continue

            services_map[service_name] = (service, None, service_name)

        # Refuse a collapsed name claimed twice
        collapsed_names = [collapsed_name for _service, _workload, collapsed_name in services_map.values()]
        if len(collapsed_names) != len(set(collapsed_names)):
            duplicates = sorted({collapsed for collapsed in collapsed_names if collapsed_names.count(collapsed) > 1})
            raise ValueError(f"the service names {duplicates} are each claimed by more than one rendered service")

        # Refuse renderings that define the reserved edge service
        if EDGE_SERVICE_NAME in collapsed_names:
            raise ValueError(f"bundle {name!r} rendering defines the reserved service {EDGE_SERVICE_NAME!r}")

        # Transform each service in place and rekey the document by collapsed name
        document.services = {}
        for service, workload, collapsed_name in services_map.values():
            document.services[collapsed_name] = service

            # Point barrier references in depends_on at the renamed barrier
            depends_on = service.depends_on
            if isinstance(depends_on, dict) and BARRIER_SERVICE in depends_on:
                depends_on[barrier] = depends_on.pop(BARRIER_SERVICE)
            elif isinstance(depends_on, list) and BARRIER_SERVICE in depends_on:
                service.depends_on = [
                    barrier if dependency == BARRIER_SERVICE else dependency for dependency in depends_on
                ]

            # Inline every bind volume as a top-level configs entry
            bind_volumes = service.strip_bind_volumes()
            if not bind_volumes:
                continue

            # Ensure the configs dict exists and the service has a configs list
            if document.configs is None:
                document.configs = {}
            if service.configs is None:
                service.configs = []

            for bind_volume in bind_volumes:
                # Resolve state-dir sources against score_dir, others against root
                path = (
                    score_dir / bind_volume.source
                    if bind_volume.source.startswith(STATE_PREFIX)
                    else root / bind_volume.source
                )

                # Double every $ except on targets the workload declared consumer-interpolated
                text = path.read_text(encoding="utf-8")
                if workload and bind_volume.target not in workload.consumer_interpolated_targets:
                    text = double_dollars(text)

                document.configs[path.name] = ComposeConfig(content=text)
                service.configs.append(ComposeConfigReference(source=path.name, target=bind_volume.target))

        # Write the transformed document
        write_generated_file(output_path, dump_compose(document.dump()))
        outputs.append(output_path)

    # Remove the compose state dir after the last entry
    shutil.rmtree(score_state, ignore_errors=True)

    # Validate the consumed secrets against the declaration
    consumed: set[str] = set()
    for output_path in outputs:
        for match in COMPOSE_HOLE.finditer(output_path.read_text(encoding="utf-8")):
            if len(match.group(1)) % 2 == 1:
                consumed.add(match.group(2))
    secrets.validate_consumed(consumed, "compose", SECRETS_FILE.as_posix())

    return outputs
