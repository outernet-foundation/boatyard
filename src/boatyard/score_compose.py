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

from boatyard.config_file import (
    COMPOSE_HOLE,
    ComposeWorkloadDocument,
    WorkloadDocument,
    Yaml,
    compose_bundle_output_path,
    double_dollars,
    lock_key_prefix,
    Config,
)
from boatyard.lock import compose_env_entries
from boatyard.lock_file import LOCK_PATH, ConfigLock
from boatyard.score import (
    SECRETS_FILE,
    SecretsDeclaration,
    dump_yaml,
    load_workloads,
    render_score,
    write_file,
)
from boatyard.values_file import ConfigValues

TOOL = "score-compose"

BARRIER_SERVICE = "wait-for-resources"
HEALTHCHECK_TEMPLATE = "probe-adapter-healthchecks.tpl"
RESTART_TEMPLATE = "restart.tpl"
GPU_COMPOSE_TEMPLATES = {"cuda": "gpu-cuda.tpl", "rocm": "gpu-rocm.tpl"}


class ComposeDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    services: dict[str, ComposeService] = Field(default_factory=dict)
    configs: dict[str, ComposeConfig] | None = None
    networks: dict[str, Yaml] | None = None

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
    networks: dict[str, Yaml] | None = None

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
    score_dir = config.root / config.score_dir
    state_prefix = f".{TOOL}/"
    images = compose_env_entries(lock, config, ConfigValues.load(config.root, config).platform)

    if "ALPINE_IMAGE" not in images:
        raise ValueError(f"{LOCK_PATH.as_posix()}: the image lock must carry the ALPINE_IMAGE entry")

    outputs: list[Path] = []
    consumed: set[str] = set()
    for name, entry in config.local_service_entries():
        workloads = load_workloads(score_dir, entry.workloads, ComposeWorkloadDocument)

        output_path = compose_bundle_output_path(config.root, name)
        tool_state = output_path.parent / f".{TOOL}"

        # Render the bundle from a clean state dir
        shutil.rmtree(tool_state, ignore_errors=True)
        render_score(
            config,
            substrate="compose",
            workloads=workloads,
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
        services_map: dict[str, tuple[ComposeService, WorkloadDocument | None, str]] = {
            (service_name := f"{Path(workload.file_name).stem}-{container_name}"): (
                document.services[service_name],
                workload.document,
                container_name,
            )
            for workload in workloads
            for container_name in workload.document.containers
        }

        # Carry workload-less services through under their rendered names
        for service_name, service in document.services.items():
            if service_name in services_map:
                continue

            # Pin the barrier image to the lock hole
            if service_name == BARRIER_SERVICE:
                barrier_service = document.services[BARRIER_SERVICE]
                barrier_service.image = "${ALPINE_IMAGE:?err}"
                services_map[BARRIER_SERVICE] = (barrier_service, None, BARRIER_SERVICE)
                continue

            services_map[service_name] = (service, None, service_name)

        # Refuse a collapsed name claimed twice
        collapsed_names = [collapsed_name for _service, _workload, collapsed_name in services_map.values()]
        if len(collapsed_names) != len(set(collapsed_names)):
            duplicates = sorted({collapsed for collapsed in collapsed_names if collapsed_names.count(collapsed) > 1})
            raise ValueError(f"the service names {duplicates} are each claimed by more than one rendered service")

        # Namespace every service under the entry with its bare name as a network alias
        network = f"{name}-net"
        collapsed = set(collapsed_names)
        document.services = {}
        document.networks = {network: {}}
        for service, workload, collapsed_name in services_map.values():
            document.services[f"{name}-{collapsed_name}"] = service
            service.networks = {network: {"aliases": [collapsed_name]}}

            # Namespace dependency keys that name rendered services of this bundle
            depends_on = service.depends_on
            if isinstance(depends_on, dict):
                service.depends_on = {
                    f"{name}-{dependency}" if dependency in collapsed else dependency: condition
                    for dependency, condition in depends_on.items()
                }
            elif isinstance(depends_on, list):
                service.depends_on = [
                    f"{name}-{dependency}" if dependency in collapsed else dependency for dependency in depends_on
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
                # Resolve state-dir sources against the tool's working directory, others against root
                path = (
                    output_path.parent / bind_volume.source
                    if bind_volume.source.startswith(state_prefix)
                    else config.root / bind_volume.source
                )

                # Double every $ except on targets the workload declared consumer-interpolated
                text = path.read_text(encoding="utf-8")
                if workload and bind_volume.target not in workload.consumer_interpolated_targets:
                    text = double_dollars(text)

                document.configs[f"{name}-{path.name}"] = ComposeConfig(content=text)
                service.configs.append(ComposeConfigReference(source=f"{name}-{path.name}", target=bind_volume.target))

        # Scan consumed secrets and namespace the lock-key holes in one pass
        prefix = lock_key_prefix(name)
        text = dump_yaml(document.dump())
        namespaced: list[str] = []
        cursor = 0
        for match in COMPOSE_HOLE.finditer(text):
            namespaced.append(text[cursor : match.start()])
            cursor = match.end()
            if len(match.group(1)) % 2 == 1:
                hole = match.group(2)
                if hole in images:
                    namespaced.append(f"{match.group(1)}{{{prefix}{hole}{match.group(3) or ''}}}")
                    continue
                consumed.add(hole)
            namespaced.append(match.group(0))
        namespaced.append(text[cursor:])
        write_file(output_path, "".join(namespaced))
        outputs.append(output_path)

        # Remove the entry's compose state dir
        shutil.rmtree(tool_state, ignore_errors=True)

    secrets.validate_consumed(consumed, "compose", SECRETS_FILE.as_posix())

    return outputs
