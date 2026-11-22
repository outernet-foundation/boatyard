from __future__ import annotations

import re
import shutil
from functools import partial
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from boatyard.markers import workload_documents
from boatyard.models.config_file import (
    BARRIER_IMAGE_SLOT,
    BARRIER_SERVICE,
    COMPOSE_SLOT,
    TREE_SENTINEL_PATTERN,
    compose_bundle_output_path,
    double_dollars,
    env_lock_slot,
    env_tree_slot,
    ConfigFile,
)
from boatyard.models.lock_file import LockFile
from boatyard.models.values_file import ValuesFile
from boatyard.models.workload import PROBE_ADAPTER_ANNOTATION, LoadedWorkload
from boatyard.paths import VALUES_FILE
from boatyard.yaml import Yaml, load_yaml_model
from boatyard.score import (
    dump_yaml,
    render_score,
    route_path_exports,
    write_file,
)

TOOL = "score-compose"


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
    annotations: dict[str, str] | None = None

    @property
    def bind_volumes(self) -> list[BindVolume]:
        # Repo-relative sources name repo files the hermetic pass inlines as configs content
        return [
            volume
            for volume in self.volumes or []
            if isinstance(volume, BindVolume) and not Path(volume.source).is_absolute()
        ]

    @property
    def host_bind_volumes(self) -> list[BindVolume]:
        # Absolute sources name host paths — authored mounts that ride through untouched
        return [
            volume
            for volume in self.volumes or []
            if isinstance(volume, BindVolume) and Path(volume.source).is_absolute()
        ]

    def strip_bind_volumes(self) -> list[BindVolume]:
        binds = self.bind_volumes
        self.volumes = [volume for volume in self.volumes or [] if volume not in binds] or None
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


def generate_compose(config: ConfigFile, lock: LockFile) -> list[Path]:
    state_prefix = f".{TOOL}/"
    values_path = config.root / VALUES_FILE
    # The values file answers only its platform here; the full validation is up's
    platform = ValuesFile.load(values_path).platform if values_path.is_file() else None
    images = lock.manifest_references(platform, config.mirror_prefix)

    # The build-set slot supply mints the barrier; a workload ${X_SHA} naming no build
    # refuses at render_score's leftover check.
    sentinels = config.tree_sentinels
    axes = config.variant_axes
    rendered_workload_names = {
        workload_name for _name, entry in config.local_service_entries() for workload_name in entry.workloads
    }
    # Plain score-compose silently drops httpGet probes; the k8s rendering renders probes
    # natively, so an httpGet without the adapter annotation refuses here
    for workload in [config.workloads[workload_name] for workload_name in sorted(rendered_workload_names)]:
        if workload.document.metadata.annotations.get(PROBE_ADAPTER_ANNOTATION) is not None:
            continue
        for container in workload.document.containers.values():
            for probe, block in (
                ("readinessProbe", container.readiness_probe),
                ("livenessProbe", container.liveness_probe),
            ):
                if block is not None and block.http_get is not None:
                    raise ValueError(
                        f"{workload.file_name}: a {probe} httpGet requires {PROBE_ADAPTER_ANNOTATION} "
                        "(the compose rendering)"
                    )
    # Route path exports are assembly-wide facts, not per-entry: a provisioner in
    # one bundle templates a claim authored in another (grafana's OAuth URLs spell
    # keycloak's claim path), so every rendering's env carries every claim.
    route_exports = route_path_exports(list(config.workloads.values()))
    documents = workload_documents(list(config.workloads.values()))
    outputs: list[Path] = []
    for name, entry in config.local_service_entries():
        workloads = [config.workloads[workload_name] for workload_name in entry.workloads]
        _refuse_colliding_spellings(name, workloads)

        output_path = compose_bundle_output_path(config.root, name)
        tool_state = output_path.parent / f".{TOOL}"

        # Render the bundle from a clean state dir
        shutil.rmtree(tool_state, ignore_errors=True)
        render_score(
            config,
            substrate="compose",
            workloads=workloads,
            documents=documents,
            route_exports=route_exports,
            variable_values={**sentinels, **entry.vars},
            templates=[
                HEALTHCHECK_TEMPLATE,
                RESTART_TEMPLATE,
                *(
                    GPU_COMPOSE_TEMPLATES[value]
                    for variable, value in entry.vars.items()
                    if variable in axes and value in GPU_COMPOSE_TEMPLATES
                ),
            ],
            output=output_path,
            images=images,
        )

        # Parse the rendering into the typed document
        document = load_yaml_model(ComposeDocument, output_path)

        # Attribute rendered container services to their owning workloads
        services_map: dict[str, tuple[ComposeService, LoadedWorkload | None, str]] = {
            (service_name := f"{Path(workload.file_name).stem}-{container_name}"): (
                document.services[service_name],
                workload,
                container_name,
            )
            for workload in workloads
            for container_name in workload.document.containers
        }

        # Carry workload-less services through under their rendered names
        for service_name, service in document.services.items():
            if service_name in services_map:
                continue

            # Pin the barrier image to the boatyard-minted slot
            if service_name == BARRIER_SERVICE:
                barrier_service = document.services[BARRIER_SERVICE]
                barrier_service.image = f"${{{BARRIER_IMAGE_SLOT}:?err}}"
                services_map[BARRIER_SERVICE] = (barrier_service, None, BARRIER_SERVICE)
                continue

            services_map[service_name] = (service, None, service_name)

        # Namespace the bundle's bare service names; one collapsed name claims one service, so
        # two workloads claiming one spelling refuse.
        claim_sites: dict[str, str] = {}
        document.services = {}
        for service, workload, collapsed_name in services_map.values():
            site = (
                f"{workload.file_name}: containers.{collapsed_name}"
                if workload is not None
                else (
                    "the reserved barrier name" if collapsed_name == BARRIER_SERVICE else "the rendered services text"
                )
            )
            if collapsed_name in claim_sites:
                raise ValueError(
                    f"services entry {name!r}: the service name {collapsed_name!r} is claimed at "
                    f"{claim_sites[collapsed_name]} and {site}"
                )
            claim_sites[collapsed_name] = site
            document.services[collapsed_name] = service

            # Inline every repo-relative bind volume as a top-level configs entry;
            # absolute-source binds are authored host mounts and stay in place
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
                consumer_interpolated = (
                    workload.document.boatyard.consumer_interpolated
                    if workload is not None and workload.document.boatyard is not None
                    else []
                )
                if workload is not None and bind_volume.target not in consumer_interpolated:
                    text = double_dollars(text)

                document.configs[path.name] = ComposeConfig(content=text)
                service.configs.append(ComposeConfigReference(source=path.name, target=bind_volume.target))

        # Rewrite the sha sentinels and namespace the lock-key slots
        text = dump_yaml(document.dump())
        text = TREE_SENTINEL_PATTERN.sub(partial(_rewrite_sentinel, name), text)
        text = COMPOSE_SLOT.sub(partial(_namespace_slot, name, images), text)
        write_file(output_path, text)
        outputs.append(output_path)

        # Remove the entry's compose state dir
        shutil.rmtree(tool_state, ignore_errors=True)

    return outputs


def _refuse_colliding_spellings(entry_name: str, workloads: list[LoadedWorkload]) -> None:
    # One workload claims each rendered spelling; score-compose keys its output services by them.
    spellings: dict[str, str] = {}
    for workload in workloads:
        for container_name in workload.document.containers:
            site = f"{workload.file_name}: containers.{container_name}"
            spelling = f"{Path(workload.file_name).stem}-{container_name}"
            if spelling in spellings:
                raise ValueError(
                    f"services entry {entry_name!r}: the rendered spelling {spelling!r} "
                    f"is claimed at {spellings[spelling]} and {site}"
                )
            spellings[spelling] = site


def _rewrite_sentinel(entry_name: str, match: re.Match[str]) -> str:
    return "${" + env_tree_slot(entry_name, match.group(1)) + "}"


def _namespace_slot(entry_name: str, images: dict[str, str], match: re.Match[str]) -> str:
    # Odd $-parity is interpolation: lock keys rename under the entry, tree slots pass through unchanged
    if len(match.group(1)) % 2 == 1:
        slot = match.group(2)
        if slot in images:
            return f"{match.group(1)}{{{env_lock_slot(entry_name, slot)}{match.group(3) or ''}}}"
    return match.group(0)
