from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)

from boatyard.compose import merged_environment
from boatyard.config import (
    COMPOSE_HOLE,
    CONSUMER_INTERPOLATED_ANNOTATION,
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

GATE_SERVICE = "wait-for-resources"
HEALTHCHECK_TEMPLATE = "probe-adapter-healthchecks.tpl"
RESTART_TEMPLATE = "restart.tpl"
GPU_COMPOSE_TEMPLATES = {"cuda": "gpu-cuda.tpl", "rocm": "gpu-rocm.tpl"}


class ComposeDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    services: dict[str, ComposeService] | None = None
    configs: dict[str, ComposeConfig] | None = None

    @model_validator(mode="after")
    def _reject_colliding_bind_basenames(self) -> Self:
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

    def dump(self) -> Yaml:
        return self.model_dump(exclude_none=True)


class ComposeService(BaseModel):
    model_config = ConfigDict(extra="allow")

    volumes: list[BindVolume | OtherVolume] | None = None
    depends_on: dict[str, Yaml] | list[str] | None = None
    configs: list[ComposeConfigReference] | None = None
    image: str | None = None


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
    # Local bundles (group options and bare entries) in declaration order.
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

        # Recreate the shared state dir and render the bundle through the spine.
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

        # Parse the rendered output into the typed compose document.
        document = ComposeDocument.model_validate(yaml.safe_load(output_path.read_text(encoding="utf-8")))
        services = document.services or {}

        # Walk each workload's declared containers: attribute the rendered
        # {workload}-{container} services, pairing each with its owning workload and
        # its collapsed name (the service name is the container name).
        services_map: dict[str, tuple[ComposeService, ComposeWorkloadDocument | None, str]] = {}
        for workload_name, workload in workloads:
            for container_name in workload.containers:
                service_name = f"{workload_name}-{container_name}"
                services_map[service_name] = (services[service_name], workload, container_name)

        # Carry services no workload owns — score-synthesized helpers and provisioner
        # services — through under their rendered names.
        for service_name, service in services.items():
            if service_name in services_map or service_name == GATE_SERVICE:
                continue
            services_map[service_name] = (service, None, service_name)

        # score synthesizes wait-for-resources, an alpine gate the bundle's services
        # depend on so nothing starts before the stack's dependencies are ready. All
        # bundle outputs merge into one compose project, so pin its bare image to the
        # ALPINE_IMAGE lock hole, rename it per entry, and follow depends_on.
        gate = f"{GATE_SERVICE}-{name}"
        if GATE_SERVICE in services:
            gate_service = services[GATE_SERVICE]
            gate_service.image = "${ALPINE_IMAGE:?err}"
            services_map[GATE_SERVICE] = (gate_service, None, gate)

        # Refuse renderings that define the reserved edge service.
        if any(collapsed_name == EDGE_SERVICE_NAME for _service, _workload, collapsed_name in services_map.values()):
            raise ValueError(f"bundle {name!r} rendering defines the reserved service {EDGE_SERVICE_NAME!r}")

        contents: dict[str, ComposeConfig] = {}
        for service, workload, _collapsed_name in services_map.values():
            inline_binds(service, workload, contents, score_dir, root)
            depends_on = service.depends_on
            if isinstance(depends_on, dict) and GATE_SERVICE in depends_on:
                depends_on[gate] = depends_on.pop(GATE_SERVICE)
            elif isinstance(depends_on, list) and GATE_SERVICE in depends_on:
                service.depends_on = [gate if dependency == GATE_SERVICE else dependency for dependency in depends_on]

        # Write the transformed document, refusing any name claimed twice.
        claimed: dict[str, str] = {}
        document_services: dict[str, ComposeService] = {}
        for service_name, (service, _workload, collapsed_name) in services_map.items():
            if collapsed_name in claimed:
                raise ValueError(
                    f"services {claimed[collapsed_name]} and {service_name} both collapse to {collapsed_name}"
                )
            claimed[collapsed_name] = service_name
            document_services[collapsed_name] = service
        document.services = document_services
        if contents:
            document.configs = {**(document.configs or {}), **contents}
        write_generated_file(output_path, dump_compose(document.dump()))
        outputs.append(output_path)

    # Remove the compose state dir after the last entry.
    shutil.rmtree(score_state, ignore_errors=True)

    # Validate the consumed secrets against the declaration.
    consumed: set[str] = set()
    for output_path in outputs:
        for match in COMPOSE_HOLE.finditer(output_path.read_text(encoding="utf-8")):
            if len(match.group(1)) % 2 == 1:
                consumed.add(match.group(2))
    secrets.validate_consumed(consumed, "compose", SECRETS_FILE.as_posix())

    return outputs


def inline_binds(
    service: ComposeService,
    workload: ComposeWorkloadDocument | None,
    contents: dict[str, ComposeConfig],
    score_dir: Path,
    root: Path,
) -> None:
    binds = [volume for volume in service.volumes or [] if isinstance(volume, BindVolume)]
    if not binds:
        return
    # Declared files of the owning workload whose contents ride un-doubled.
    interpolated_targets = (
        workload.metadata.annotations[CONSUMER_INTERPOLATED_ANNOTATION].split(",")
        if workload is not None and CONSUMER_INTERPOLATED_ANNOTATION in workload.metadata.annotations
        else []
    )
    references: list[ComposeConfigReference] = []
    for bind in binds:
        # Resolve state-dir sources against score_dir, other sources against the repo root.
        path = score_dir / bind.source if bind.source.startswith(STATE_PREFIX) else root / bind.source
        text = path.read_text(encoding="utf-8")
        if bind.target not in interpolated_targets:
            text = double_dollars(text)
        contents[path.name] = ComposeConfig(content=text)
        references.append(ComposeConfigReference(source=path.name, target=bind.target))
    service.configs = [*(service.configs or []), *references]
    service.volumes = [volume for volume in service.volumes or [] if not isinstance(volume, BindVolume)] or None
