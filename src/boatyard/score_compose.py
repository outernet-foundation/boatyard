from __future__ import annotations

import re
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
    compose_bundle_output,
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

DOUBLED_SERVICE_NAME = re.compile(r"^(.+)-\1$")

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
        # Collect per-workload annotation facts and enforce the compose placement laws.
        interpolated_targets: set[str] = set()
        for workload_name in entry.workloads:
            workload = ComposeWorkloadDocument.load(score_dir / workload_name)
            annotations = workload.metadata.annotations

            # Track the declared files whose contents ride un-doubled.
            consumer_interpolated = annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
            if consumer_interpolated is not None:
                interpolated_targets.update(consumer_interpolated.split(","))

        # Assemble the patch templates, adding the GPU template for the bundle's variant.
        templates = [HEALTHCHECK_TEMPLATE, RESTART_TEMPLATE]
        gpu_template = GPU_COMPOSE_TEMPLATES.get(entry.vars.get("GPU_VARIANT", ""))
        if gpu_template is not None:
            templates.append(gpu_template)

        # Recreate the shared state dir and render the bundle through the spine.
        shutil.rmtree(score_state, ignore_errors=True)
        output = compose_bundle_output(root, name)
        render_score(
            config,
            is_k8s=False,
            workloads=entry.workloads,
            rendering_vars=entry.vars,
            templates=templates,
            output=output,
            images=images,
            service_shas=service_shas,
        )

        # Parse the rendered output into the typed compose document.
        raw = yaml.safe_load(output.read_text(encoding="utf-8"))
        document = ComposeDocument.model_validate(raw)
        services = document.services or {}

        # Collapse doubled {workload}-{container} service names.
        collapsed: dict[str, ComposeService] = {}
        for service_name, service in services.items():
            match = DOUBLED_SERVICE_NAME.fullmatch(service_name)
            target = match.group(1) if match is not None else service_name
            if target != service_name and target in services:
                raise ValueError(f"service rename {service_name} -> {target} collides with an existing service")
            collapsed[target] = service

        # score synthesizes wait-for-resources, an alpine gate the bundle's services
        # depend on so nothing starts before the stack's dependencies are ready. All
        # bundle outputs merge into one compose project, so pin its bare image to the
        # ALPINE_IMAGE lock hole, rename it per entry, and follow depends_on.
        gate = f"{GATE_SERVICE}-{name}"
        if GATE_SERVICE in collapsed:
            if gate in collapsed:
                raise ValueError(f"gate rename {GATE_SERVICE} -> {gate} collides with an existing service")
            gate_service = collapsed.pop(GATE_SERVICE)
            gate_service.image = "${ALPINE_IMAGE:?err}"
            collapsed[gate] = gate_service
            for service in collapsed.values():
                depends_on = service.depends_on
                if isinstance(depends_on, dict) and GATE_SERVICE in depends_on:
                    depends_on[gate] = depends_on.pop(GATE_SERVICE)
                elif isinstance(depends_on, list) and GATE_SERVICE in depends_on:
                    service.depends_on = [
                        gate if dependency == GATE_SERVICE else dependency for dependency in depends_on
                    ]

        # Refuse renderings that define the reserved edge service.
        if EDGE_SERVICE_NAME in collapsed:
            raise ValueError(f"bundle {name!r} rendering defines the reserved service {EDGE_SERVICE_NAME!r}")

        # Inline every bind as configs.content with $ doubled.
        contents: dict[str, ComposeConfig] = {}
        for service in collapsed.values():
            binds = [volume for volume in service.volumes or [] if isinstance(volume, BindVolume)]
            if not binds:
                continue
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
            remaining: list[BindVolume | OtherVolume] = [
                volume for volume in service.volumes or [] if not isinstance(volume, BindVolume)
            ]
            service.volumes = remaining or None

        # Write the transformed document.
        document.services = collapsed
        if contents:
            document.configs = {**(document.configs or {}), **contents}
        write_generated_file(output, dump_compose(document.dump()))
        outputs.append(output)

    # Remove the compose state dir after the last entry.
    shutil.rmtree(score_state, ignore_errors=True)

    # Validate the consumed secrets against the declaration.
    consumed: set[str] = set()
    for output in outputs:
        for match in COMPOSE_HOLE.finditer(output.read_text(encoding="utf-8")):
            if len(match.group(1)) % 2 == 1:
                consumed.add(match.group(2))
    secrets.validate_consumed(consumed, "compose", SECRETS_FILE.as_posix())

    return outputs
