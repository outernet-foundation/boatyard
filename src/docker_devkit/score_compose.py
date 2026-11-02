from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Literal, Self

import yaml
from bashrun.bash import bash
from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_validator,
)

from docker_devkit.config import (
    CONSUMER_INTERPOLATED_ANNOTATION,
    DEV_TOGGLES_ANNOTATION,
    EDGE_SERVICE_NAME,
    PROBE_ADAPTER_ANNOTATION,
    WorkloadDocument,
    Yaml,
    double_dollars,
    is_mapping,
    validate,
    ComposeBundle,
    Config,
)
from docker_devkit.config_lock import LOCK_PATH
from docker_devkit.score import (
    builtin_patch_templates,
    compiled_provisioners,
    dump_compose,
    init_score_tool,
    load_yaml_file,
    rendered_workloads,
    write_generated_file,
)

STATE_PREFIX = ".score-compose/"

# Match doubled {workload}-{container} service names.
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
        # Refuse bind sources sharing one basename.
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
        # Declared fields dump before extras; this order is the artifacts' deterministic key order.
        return self.model_dump(exclude_none=True)


class ComposeService(BaseModel):
    model_config = ConfigDict(extra="allow")

    volumes: list[BindVolume | OtherVolume] | None = None
    depends_on: dict[str, Yaml] | list[str] | None = None
    configs: list[ComposeConfigReference] | None = None


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

    # Reject malformed binds.
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
    root: Path,
    config: Config,
    images: dict[str, str],
    service_shas: dict[str, str],
    tool_environment: dict[str, str] | None,
) -> None:
    bundles = config.compose_bundles()
    if not bundles:
        raise ValueError("the compose target requires local service entries (workloads:)")
    score_dir = config.root / config.score_dir
    score_state = score_dir / STATE_PREFIX

    with builtin_patch_templates() as builtin_templates:
        for bundle in bundles:
            interpolated_targets: set[str] = set()
            toggle_services: dict[str, str] = {}
            for name in bundle.workloads:
                workload = validate(WorkloadDocument, load_yaml_file(score_dir / name), name)
                annotations = workload.metadata.annotations

                consumer_interpolated = annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
                if consumer_interpolated is not None:
                    interpolated_targets.update(consumer_interpolated.split(","))

                # The httpGet requirement is a compose placement law: score-compose drops httpGet probes.
                if annotations.get(PROBE_ADAPTER_ANNOTATION) is None:
                    for probe in workload.http_get_probes:
                        raise ValueError(f"{name}: a {probe} httpGet requires {PROBE_ADAPTER_ANNOTATION}")

                toggle_raw, service_name = workload.dev_toggles
                if toggle_raw is not None and service_name is not None:
                    if service_name in toggle_services:
                        raise ValueError(
                            f"{toggle_services[service_name]} and {name} both declare "
                            f"{DEV_TOGGLES_ANNOTATION} for {service_name}"
                        )
                    toggle_services[service_name] = name
            with compiled_provisioners(score_dir, "compose", bundle.workloads, root, images) as provisioners:
                if provisioners.declares_services and "ALPINE_IMAGE" not in images:
                    raise ValueError(
                        f"bundle {bundle.name!r}: its provisioners declare services; "
                        f"{LOCK_PATH.as_posix()} must carry the ALPINE_IMAGE entry"
                    )
                templates = _compose_templates(builtin_templates, bundle)
                shutil.rmtree(score_state, ignore_errors=True)
                init_score_tool(
                    "score-compose",
                    score_dir,
                    config.project_name,
                    provisioners.paths,
                    templates,
                    tool_environment,
                )

                output = bundle.output
                output.parent.mkdir(parents=True, exist_ok=True)
                with rendered_workloads(config, bundle.workloads, bundle.vars, images, service_shas) as workloads:
                    bash(
                        f"score-compose generate {workloads} --output {output.as_posix()}",
                        cwd=score_dir,
                        env=tool_environment,
                    )

                raw = yaml.safe_load(output.read_text(encoding="utf-8"))
                # Score-compose synthesizes the gate with a bare alpine image; pin it to the ALPINE_IMAGE lock hole.
                raw_services = raw.get("services") if is_mapping(raw) else None
                gate_service = raw_services.get(GATE_SERVICE) if is_mapping(raw_services) else None
                if is_mapping(gate_service):
                    gate_service["image"] = "${ALPINE_IMAGE:?err}"
                # Post-passes run as typed transformations; the model dumps exactly once.
                document = validate(ComposeDocument, raw, output.as_posix())
                services = document.services or {}

                # Collapse doubled {workload}-{container} service names.
                collapsed: dict[str, ComposeService] = {}
                for name, service in services.items():
                    match = DOUBLED_SERVICE_NAME.fullmatch(name)
                    target = match.group(1) if match is not None else name
                    if target != name and target in services:
                        raise ValueError(f"service rename {name} -> {target} collides with an existing service")
                    collapsed[target] = service

                # Rename the gate per entry and follow the depends_on references.
                gate = f"{GATE_SERVICE}-{bundle.name}"
                if GATE_SERVICE in collapsed:
                    if gate in collapsed:
                        raise ValueError(f"gate rename {GATE_SERVICE} -> {gate} collides with an existing service")
                    collapsed[gate] = collapsed.pop(GATE_SERVICE)
                    for service in collapsed.values():
                        depends_on = service.depends_on
                        if isinstance(depends_on, dict) and GATE_SERVICE in depends_on:
                            depends_on[gate] = depends_on.pop(GATE_SERVICE)
                        elif isinstance(depends_on, list) and GATE_SERVICE in depends_on:
                            service.depends_on = [
                                gate if dependency == GATE_SERVICE else dependency for dependency in depends_on
                            ]

                if EDGE_SERVICE_NAME in collapsed:
                    raise ValueError(
                        f"bundle {bundle.name!r} rendering defines the reserved service {EDGE_SERVICE_NAME!r}"
                    )

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

                document.services = collapsed
                if contents:
                    document.configs = {**(document.configs or {}), **contents}
                write_generated_file(output, dump_compose(document.dump()))

    shutil.rmtree(score_state, ignore_errors=True)


def _compose_templates(builtin_templates: Path, bundle: ComposeBundle) -> list[Path]:
    names = [HEALTHCHECK_TEMPLATE, RESTART_TEMPLATE]
    gpu_template = GPU_COMPOSE_TEMPLATES.get(bundle.vars.get("GPU_VARIANT", ""))
    if gpu_template is not None:
        names.append(gpu_template)
    return [builtin_templates / name for name in names]
