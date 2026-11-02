from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from functools import partial
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, NamedTuple, Self

import yaml
from bashrun.bash import bash, bash_output
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationInfo,
    field_validator,
    model_validator,
)

from docker_devkit.config import (
    DEV_TOGGLES_ANNOTATION,
    EDGE_SERVICE_NAME,
    ENV_NAME_PATTERN,
    K8sConfig,
    Target,
    is_list,
    is_mapping,
    parse_dev_toggles,
    DockerDevkitConfig,
    chart_lock_key,
)
from docker_devkit.config_lock import LOCK_PATH, ImageLock, parse_image_lock
from docker_devkit.documents import BindVolume, ComposeDocument, WorkloadDocument
from docker_devkit.assembly import merged_environment
from docker_devkit.lock import compose_env_entries, env_entries

# Match only ALL-CAPS placeholders so Score's lowercase dotted references never collide.
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

# Treat sources under this prefix as relative to score-compose's state directory.
STATE_PREFIX = ".score-compose/"

# score-compose names services {workload}-{container}; when the halves match, the name is doubled.
DOUBLED_SERVICE_NAME = re.compile(r"^(.+)-\1$")

# Count a brace hole as consumed when an odd number of dollars precedes it, since even runs
# are $$-escaped literals.
COMPOSE_HOLE = re.compile(r"(\$+)\{([A-Z][A-Z0-9_]*)(?::[^{}]*)?\}")

# Built-in patch templates ship as package data under this directory and register onto the
# devkit's own score init invocations; consumer repos never author or declare one.
PATCH_TEMPLATES_DIR = "patch_templates"
GATE_PIN_TEMPLATE = "wait-for-resources-image.tpl"
GATE_SERVICE = "wait-for-resources"
HEALTHCHECK_TEMPLATE = "probe-adapter-healthchecks.tpl"
RESTART_TEMPLATE = "restart.tpl"
GPU_POD_TEMPLATE = "gpu-pod.tpl"
SCALE_TO_ZERO_TEMPLATE = "scale-to-zero.tpl"
GPU_COMPOSE_TEMPLATES = {"cuda": "gpu-cuda.tpl", "rocm": "gpu-rocm.tpl"}

# The deployment-surface annotations the built-in templates read; their refusal laws live
# in devkit code because templates are data and cannot refuse.
PROBE_ADAPTER_ANNOTATION = "outernet.internal/probe-adapter"
PROBE_ADAPTERS = ("python-urllib", "bash-http")
RESTART_ANNOTATION = "outernet.internal/restart"
COMPOSE_RESTART_POLICIES = ("no", "always", "on-failure", "unless-stopped")
PUBLISH_ANNOTATION = "outernet.internal/publish"
CONSUMER_INTERPOLATED_ANNOTATION = "outernet.internal/consumer-interpolated"

# The storage classes are org-global infra facts, one k8s consumer; score-k8s defines the
# variable name the workload files interpolate.
STORAGE_CLASS_VAR = "SCORE_STORAGE_CLASS"
CLOUD_STORAGE_CLASS = "hcloud-volumes"
LOCAL_STORAGE_CLASS = "local-path"

SECRETS_FILE = Path("stack/secrets.yaml")
SECRETS_OUTPUT = Path("stack/generated/secrets.txt")

# Compile these fields from authored mappings into the YAML strings score's Go templates
# consume; authored manifests stay a real list.
STRING_FORM_FIELDS = ("state", "outputs", "volumes", "services", "manifests")

# Authoring markers are single-key mappings so authored sources stay plain-YAML-parseable.
# Include markers insert a repo-root-relative file's bytes or parsed structure; lock markers
# substitute image-lock entries. Include resolution runs before lock resolution, and text
# content is never scanned for markers.
TEXT_MARKER = "include"
YAML_MARKER = "include-yaml"
LOCK_MARKER = "lock"
LOCK_IMAGE_MARKER = "lock-image"
INCLUDE_MARKERS = (TEXT_MARKER, YAML_MARKER)
LOCK_MARKERS = (LOCK_MARKER, LOCK_IMAGE_MARKER)
MARKERS = INCLUDE_MARKERS + LOCK_MARKERS
DIGEST_FIELDS = ("digest", "sha")
IMAGE_FIELD = "image"
IMAGE_HOLE_PATTERN = re.compile(r"\$\{|{{")


class ManifestMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str = ""
    name: str = ""


class ManifestDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str = ""
    metadata: ManifestMetadata = Field(default_factory=ManifestMetadata)


class WorkloadState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    instance_suffix: str = ""


class ScoreState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workloads: dict[str, WorkloadState] = {}


class SecretKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    mint: bool = False

    @field_validator("description")
    @classmethod
    def _require_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("every declared key needs a one-line description")
        return value


class SecretsDeclaration(RootModel[dict[str, SecretKey]]):
    @model_validator(mode="before")
    @classmethod
    def _coerce_string_specs(cls, value: object, info: ValidationInfo) -> object:
        context: dict[str, object] = info.context or {}
        source = context.get("source", "the secrets declaration")
        if not is_mapping(value) or not value:
            raise ValueError(f"{source}: the secrets declaration is a non-empty KEY: description mapping")
        return {name: ({"description": spec} if isinstance(spec, str) else spec) for name, spec in value.items()}

    @model_validator(mode="after")
    def _require_all_caps_keys(self, info: ValidationInfo) -> Self:
        context: dict[str, object] = info.context or {}
        source = context.get("source", "the secrets declaration")
        for name in self.root:
            if not ENV_NAME_PATTERN.match(name):
                raise ValueError(f"{source}: {name!r} is not an ALL_CAPS key")
        return self

    def validate_consumed(self, consumed: set[str], rendering: str, source: str) -> None:
        undeclared = sorted(consumed - self.root.keys())
        if undeclared:
            raise RuntimeError(
                f"{source}: the {rendering} rendering consumes keys missing from the secrets declaration: "
                + ", ".join(undeclared)
            )


class ComposeDumper(yaml.SafeDumper):
    pass


def _represent_multiline(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # Force literal block style for multiline strings, building the node directly to satisfy strict typing.
    return yaml.ScalarNode("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


ComposeDumper.add_representer(str, _represent_multiline)


class LockImageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    digest_field: str = "digest"

    @field_validator("digest_field")
    @classmethod
    def _require_known_digest_field(cls, value: str) -> str:
        if value not in DIGEST_FIELDS:
            raise ValueError(f"digest_field must be one of {DIGEST_FIELDS}")
        return value


class AuthoredProvisioner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    type: str
    description: str = ""
    state: dict[str, object] | None = None
    outputs: dict[str, object] | None = None
    volumes: dict[str, object] | None = None
    services: dict[str, object] | None = None
    manifests: list[object] | None = None

    @field_validator(*STRING_FORM_FIELDS, mode="before")
    @classmethod
    def _reject_string_form(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, str):
            return value

        shape = "list" if info.field_name == "manifests" else "mapping"
        context: dict[str, object] = info.context or {}
        raise ValueError(
            f"{context.get('name', 'the provisioner file')}: "
            f"{info.field_name} is already in score's compiled string form; author it as a {shape}"
        )


class CompiledProvisioners(NamedTuple):
    # declares_services is the phantom-service condition: score-compose synthesizes its
    # wait-for-resources gate exactly when provisioner-added services exist, and the
    # devkit's built-in gate-pin template registers on it.
    paths: list[Path]
    declares_services: bool


def generate_artifacts(config: DockerDevkitConfig, target: Target, local: bool) -> list[Path]:
    # Resolve image tags from the committed tree, exactly as up and build do; the artifacts
    # embed tree tags, so the build keys are required.
    if not (config.repository or config.base_images):
        raise RuntimeError(
            "the build keys (repository or base_images) are required in docker-devkit.yaml — "
            "score artifacts embed the tree tags they derive"
        )
    assert config.root is not None
    root = config.root
    os.environ.update(config.service_shas())
    if config.k8s is not None:
        os.environ[STORAGE_CLASS_VAR] = LOCAL_STORAGE_CLASS if local else CLOUD_STORAGE_CLASS
    # The lock beside the manifest is the single digest source for authored lock markers.
    lock_path = root / LOCK_PATH
    if not lock_path.is_file():
        raise RuntimeError(f"{lock_path.as_posix()}: the image lock is required for generation (lock writes it)")
    lock = parse_image_lock(lock_path)
    if not lock.images and not lock.charts:
        raise RuntimeError(f"{lock_path.as_posix()}: the image lock carries no entries")
    k8s_output = root / (config.k8s_local_output if local else config.k8s_output)

    outputs: list[Path] = []

    if target in ("both", "compose"):
        # Compose markers resolve to the platform manifests of the selected platform —
        # the committed artifact records the exact bytes one machine runs.
        generate_compose(config, compose_env_entries(lock, config, merged_environment(root)))
        outputs.extend(bundle.output for bundle in config.compose_bundles())

    if target in ("both", "k8s") and config.k8s is not None:
        generate_k8s(config, config.k8s, k8s_output, lock)
        outputs.append(k8s_output)

    if (root / SECRETS_FILE).is_file():
        # The secrets contract: validate each rendering this run generated against the
        # declaration, and derive the mint list from the declaration alone. The cluster
        # Secret name derives from the compose project name.
        declaration_path = root / SECRETS_FILE
        k8s_secret = f"{config.project_name}-secrets"

        declaration = SecretsDeclaration.model_validate(
            yaml.safe_load(declaration_path.read_text(encoding="utf-8")),
            context={"source": declaration_path.as_posix()},
        )

        if target in ("both", "compose"):
            consumed: set[str] = set()
            for bundle in config.compose_bundles():
                for match in COMPOSE_HOLE.finditer(bundle.output.read_text(encoding="utf-8")):
                    if len(match.group(1)) % 2 == 1:
                        consumed.add(match.group(2))
            declaration.validate_consumed(consumed, "compose", SECRETS_FILE.as_posix())

        if target in ("both", "k8s") and config.k8s is not None:
            consumed = set()
            for block in _split_documents(k8s_output.read_text(encoding="utf-8")):
                collect_secret_keys(yaml.safe_load(block), k8s_secret, consumed)
            declaration.validate_consumed(consumed, "k8s", SECRETS_FILE.as_posix())

        output = root / SECRETS_OUTPUT
        output.parent.mkdir(parents=True, exist_ok=True)
        write_generated_file(
            output,
            "# Generated by generate-score from the secrets declaration\n"
            + "".join(f"{name}\n" for name in sorted(name for name, key in declaration.root.items() if key.mint)),
        )
        outputs.append(output)

    return outputs


def generate_compose(config: DockerDevkitConfig, images: dict[str, str]) -> None:
    assert config.root is not None
    root = config.root
    bundles = config.compose_bundles()
    if not bundles:
        raise RuntimeError(
            "the compose target requires local service entries (workloads:) under docker-devkit.yaml's services key"
        )
    score_dir = config.score_dir_path()
    score_state = score_dir / STATE_PREFIX

    with _builtin_patch_templates() as builtin_templates:
        for bundle in bundles:
            interpolated_targets: set[str] = set()
            toggle_services: dict[str, str] = {}
            for name in bundle.workloads:
                document = _load_yaml_file(score_dir / name)
                workload = WorkloadDocument.model_validate(document)
                annotations = workload.metadata.annotations
                containers: dict[str, Any] = document.get("containers") or {}
                # The deployment-surface refusal laws the shipped templates cannot carry.
                adapter = annotations.get(PROBE_ADAPTER_ANNOTATION)
                if adapter is not None and adapter not in PROBE_ADAPTERS:
                    raise SystemExit(
                        f"{name}: {PROBE_ADAPTER_ANNOTATION} is {adapter!r}; the supported adapters are {list(PROBE_ADAPTERS)}"
                    )
                restart = annotations.get(RESTART_ANNOTATION)
                if restart is not None and restart not in COMPOSE_RESTART_POLICIES:
                    raise SystemExit(
                        f"{name}: {RESTART_ANNOTATION} is {restart!r}; the compose restart policies are "
                        f"{list(COMPOSE_RESTART_POLICIES)}"
                    )
                if annotations.get(PUBLISH_ANNOTATION) is not None:
                    raise SystemExit(
                        f"{name}: {PUBLISH_ANNOTATION} died — host publishes are consumer config now, "
                        "declared as ports under docker-devkit.yaml's services key"
                    )
                consumer_interpolated = annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
                if consumer_interpolated is not None:
                    if not isinstance(consumer_interpolated, str) or not consumer_interpolated.strip():
                        raise SystemExit(
                            f"{name}: {CONSUMER_INTERPOLATED_ANNOTATION} must be a non-empty comma-separated "
                            "list of the workload's files: paths"
                        )
                    declared_files: set[str] = set()
                    for container in containers.values():
                        files: dict[str, Any] = container.get("files") or {}
                        declared_files.update(files)
                    for entry in consumer_interpolated.split(","):
                        if entry != entry.strip() or not entry:
                            raise SystemExit(
                                f"{name}: {CONSUMER_INTERPOLATED_ANNOTATION} entry {entry!r} is empty or whitespace-padded"
                            )
                        if entry not in declared_files:
                            raise SystemExit(
                                f"{name}: {CONSUMER_INTERPOLATED_ANNOTATION} names {entry!r}, "
                                "which no container's files: declares"
                            )
                        interpolated_targets.add(entry)
                if adapter is None:
                    # score-compose drops httpGet probes, so compose healthchecks exist only
                    # through the adapter's conversion — a probe without one is the silent-
                    # drop failure mode.
                    for container in containers.values():
                        for probe in ("readinessProbe", "livenessProbe"):
                            probe_block: dict[str, Any] = container.get(probe) or {}
                            if probe_block.get("httpGet") is not None:
                                raise SystemExit(
                                    f"{name}: a {probe} httpGet exists but {PROBE_ADAPTER_ANNOTATION} is not "
                                    "declared; score-compose drops httpGet probes, so the compose healthcheck "
                                    "needs the adapter"
                                )
                # The dev-overlay spelling laws, refused at generation: the annotation is
                # consumed at `up --dev`, and the declaring change is where malformed
                # spelling should fail.
                toggle_raw, service_name = workload.dev_toggles
                if toggle_raw is not None:
                    if not isinstance(toggle_raw, str) or not toggle_raw.strip():
                        raise SystemExit(
                            f"{name}: {DEV_TOGGLES_ANNOTATION} must be a comma-separated NAME=default string"
                        )
                    if not isinstance(service_name, str) or not service_name:
                        raise SystemExit(f"{name}: metadata.name is required to key the dev overlay service")
                    parse_dev_toggles(name, toggle_raw)
                    if service_name in toggle_services:
                        raise SystemExit(
                            f"{toggle_services[service_name]} and {name} both declare "
                            f"{DEV_TOGGLES_ANNOTATION} for {service_name}"
                        )
                    toggle_services[service_name] = name
            provisioner_names = closure_provisioners(score_dir, "compose", bundle.workloads)
            # The state dir is re-created per bundle so one bundle's provisioners and patch
            # templates never leak into another's output.
            with compiled_provisioners(score_dir, provisioner_names, root, images) as provisioners:
                # The wait-for-resources gate exists exactly when provisioner-added services do,
                # and the built-in gate-pin template rewrites its image through the lock — so a
                # gate-forming run without the lock entry refuses here instead of shipping a
                # bare `alpine` in the artifact (templates are data and cannot refuse).
                if provisioners.declares_services and "ALPINE_IMAGE" not in images:
                    raise RuntimeError(
                        f"bundle {bundle.name!r}: its provisioners declare services, so score-compose "
                        f"synthesizes a wait-for-resources gate; {LOCK_PATH.as_posix()} lacks the "
                        "ALPINE_IMAGE entry the gate pin needs (lock writes it)"
                    )
                templates = [
                    builtin_templates / HEALTHCHECK_TEMPLATE,
                    builtin_templates / RESTART_TEMPLATE,
                ]
                if provisioners.declares_services:
                    templates.append(builtin_templates / GATE_PIN_TEMPLATE)
                gpu_template = GPU_COMPOSE_TEMPLATES.get(bundle.vars.get("GPU_VARIANT", ""))
                if gpu_template is not None:
                    templates.append(builtin_templates / gpu_template)
                shutil.rmtree(score_state, ignore_errors=True)
                _init_state(
                    f"score-compose init --project {config.project_name} --no-sample",
                    provisioners.paths,
                    templates,
                    working_dir=score_dir,
                )

                output = bundle.output
                output.parent.mkdir(parents=True, exist_ok=True)
                with _rendered_workloads(config, bundle.workloads, bundle.vars, images) as workloads:
                    bash(
                        f"score-compose generate {workloads} --output {output.as_posix()}",
                        cwd=score_dir,
                    )

                # Collapse doubled {workload}-{container} service names (api-api -> api) so bundle
                # artifacts carry the fleet-wide vocabulary; references never use the doubled form,
                # and a collapse onto an existing service name is a loud collision.
                document = yaml.safe_load(output.read_text(encoding="utf-8"))
                services: dict[str, Any] = document.get("services") or {}
                collapsed: dict[str, Any] = {}
                for name, service in services.items():
                    match = DOUBLED_SERVICE_NAME.fullmatch(name)
                    target = match.group(1) if match is not None else name
                    if target != name and target in services:
                        raise SystemExit(f"service rename {name} -> {target} collides with an existing service")
                    collapsed[target] = service
                document["services"] = collapsed

                # score-compose synthesizes the gate under one fixed name per rendering, and
                # compose includes merge same-name services silently — so each entry's
                # artifact names its own gate and the depends_on references follow.
                gate = f"{GATE_SERVICE}-{bundle.name}"
                if GATE_SERVICE in collapsed:
                    if gate in collapsed:
                        raise SystemExit(f"gate rename {GATE_SERVICE} -> {gate} collides with an existing service")
                    collapsed[gate] = collapsed.pop(GATE_SERVICE)
                    for service in collapsed.values():
                        depends_on: dict[str, Any] | list[str] | None = service.get("depends_on")
                        if isinstance(depends_on, dict) and GATE_SERVICE in depends_on:
                            depends_on[gate] = depends_on.pop(GATE_SERVICE)
                        elif isinstance(depends_on, list) and GATE_SERVICE in depends_on:
                            service["depends_on"] = [
                                gate if dependency == GATE_SERVICE else dependency for dependency in depends_on
                            ]

                # compose merges same-name services across includes silently, and the emitted
                # edge service owns the reserved name — a rendering squatting on it refuses here.
                if EDGE_SERVICE_NAME in collapsed:
                    raise SystemExit(
                        f"bundle {bundle.name!r} rendering defines the reserved service {EDGE_SERVICE_NAME!r}"
                    )

                # Inline every bind as configs.content with $ doubled, leaving zero host-filesystem references.
                parsed = ComposeDocument.model_validate(document)
                contents: dict[str, dict[str, str]] = {}
                for name, service in (parsed.services or {}).items():
                    binds = [volume for volume in service.volumes or [] if isinstance(volume, BindVolume)]
                    if not binds:
                        continue
                    references: list[dict[str, str]] = []
                    for bind in binds:
                        # Resolve state-dir sources against score_dir, other sources against the repo root.
                        path = score_dir / bind.source if bind.source.startswith(STATE_PREFIX) else root / bind.source
                        text = path.read_text(encoding="utf-8")
                        if bind.target not in interpolated_targets:
                            # App-owned text doubles $ so compose interpolation leaves it alone; a
                            # file named by the consumer-interpolation annotation rides verbatim,
                            # its single-$ holes becoming the include consumer's interpolation
                            # surface.
                            text = text.replace("$", "$$")
                        contents[path.name] = {"content": text}
                        references.append({"source": path.name, "target": bind.target})
                    raw = document["services"][name]
                    raw["configs"] = [*(raw.get("configs") or []), *references]
                    remaining = [volume for volume in raw["volumes"] if volume.get("type") != "bind"]
                    if remaining:
                        raw["volumes"] = remaining
                    else:
                        del raw["volumes"]

                if contents:
                    document["configs"] = {**(document.get("configs") or {}), **contents}
                write_generated_file(output, dump_compose(document))

    # The compose state dir is per-run temp; deleting it keeps the next run provisioning from scratch.
    shutil.rmtree(score_state, ignore_errors=True)


@contextmanager
def _builtin_patch_templates() -> Generator[Path]:
    # importlib.resources is the only resolver that finds the assets in wheel, sdist, and
    # editable installs alike.
    with as_file(files("docker_devkit") / PATCH_TEMPLATES_DIR) as directory:
        yield directory


def closure_provisioners(score_dir: Path, rendering: str, workloads: list[str]) -> list[str]:
    # A rendering registers exactly the provisioner files whose types its workloads
    # reference; orphan files (a type nothing references) silently no-ship.
    referenced: set[str] = set()
    for name in workloads:
        document: Any = _load_yaml_file(score_dir / name)
        resources = document.get("resources") if is_mapping(document) else None
        if is_mapping(resources):
            for resource in resources.values():
                resource_type = resource.get("type") if is_mapping(resource) else None
                if isinstance(resource_type, str):
                    referenced.add(resource_type)
    selected: list[str] = []
    directory = score_dir / rendering
    if not directory.is_dir():
        return selected
    for path in sorted(directory.glob("*.yaml")):
        entries: Any = _load_yaml_file(path)
        if not is_list(entries):
            continue
        if any(is_mapping(entry) and entry.get("type") in referenced for entry in entries):
            selected.append(f"{rendering}/{path.name}")
    return selected


@contextmanager
def compiled_provisioners(
    source_dir: Path, names: list[str], root: Path, images: dict[str, str]
) -> Generator[CompiledProvisioners]:
    # Compile into a per-run tempdir under source_dir: the string form never touches the
    # committed tree, and --provisioners paths stay repo-relative.
    if not names:
        yield CompiledProvisioners(paths=[], declares_services=False)
        return

    with TemporaryDirectory(prefix="compiled-provisioners-", dir=source_dir) as temporary_directory:
        directory = Path(temporary_directory)
        paths: list[Path] = []
        declares_services = False
        for name in names:
            document = resolve_lock_refs(
                resolve_includes(yaml.safe_load((source_dir / name).read_text(encoding="utf-8")), root, name),
                images,
                name,
            )
            if not is_list(document) or not document:
                raise RuntimeError(
                    f"{name}: an authored provisioner file is a non-empty YAML list of provisioner entries"
                )
            compiled_entries: list[dict[str, object]] = []
            for entry in document:
                AuthoredProvisioner.model_validate(entry, context={"name": name})
                assert is_mapping(entry)
                if entry.get("services"):
                    declares_services = True

                # Compile from the raw authored dicts so authored key order controls dump order.
                compiled: dict[str, object] = {}
                for key, value in entry.items():
                    if key in STRING_FORM_FIELDS:
                        if value is not None:
                            compiled[key] = dump_compose(value)
                    else:
                        compiled[key] = value
                compiled_entries.append(compiled)
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dump_compose(compiled_entries), encoding="utf-8", newline="")
            paths.append(target)
        yield CompiledProvisioners(paths=paths, declares_services=declares_services)


def generate_k8s(config: DockerDevkitConfig, k8s: K8sConfig, output_path: Path, lock: ImageLock) -> None:
    # Run score-k8s in the output's directory; it writes its .score-k8s state under its cwd.
    # The output arrives root-absolute (the joined k8s output path).
    assert config.root is not None
    root = config.root
    images = env_entries(lock)
    state_dir = output_path.parent / ".score-k8s"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    score_dir = config.score_dir_path()

    compose_workloads = {name for bundle in config.compose_bundles() for name in bundle.workloads}
    for name in k8s.workloads:
        if name in compose_workloads:
            continue
        k8s_document: Any = _load_yaml_file(score_dir / name)
        k8s_annotations = WorkloadDocument.model_validate(k8s_document).metadata.annotations
        if DEV_TOGGLES_ANNOTATION in k8s_annotations:
            raise SystemExit(
                f"{name}: {DEV_TOGGLES_ANNOTATION} is compose dev-overlay vocabulary; this workload "
                "renders k8s-only, so the annotation can never apply"
            )

    with (
        _builtin_patch_templates() as builtin_templates,
        compiled_provisioners(
            score_dir, closure_provisioners(score_dir, "k8s", k8s.workloads), root, images
        ) as provisioners,
    ):
        _init_state(
            "score-k8s init --no-sample",
            provisioners.paths,
            [
                builtin_templates / GPU_POD_TEMPLATE,
                builtin_templates / SCALE_TO_ZERO_TEMPLATE,
            ],
            working_dir=output_path.parent,
        )

        with _rendered_workloads(config, k8s.workloads, k8s.vars, images) as workloads:
            bash(f"score-k8s generate {workloads} --output {output_path.as_posix()}", cwd=output_path.parent)

    # Render charts through helm template into the same sorted documents; the lock is the
    # digest source, so helm renders the org-mirrored digest consumers pull — helm is a
    # renderer, never a deployer.
    if k8s.charts:
        chart_blocks: list[str] = []
        with TemporaryDirectory(prefix="resolved-values-") as values_directory:
            values_dir = Path(values_directory)
            for chart_name in k8s.charts:
                key = chart_lock_key(chart_name)
                entry = lock.require(key, f"chart {chart_name!r}")
                # Pass a marker-free values file through untouched; resolve markers into a tempdir copy.
                values_arguments: list[str] = []
                path = config.chart_values_path(chart_name)
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not contains_marker(document):
                    values_arguments.append(path.as_posix())
                else:
                    resolved = resolve_lock_refs(
                        resolve_includes(document, root, path.as_posix()), images, path.as_posix()
                    )
                    target = values_dir / path.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    write_generated_file(target, dump_compose(resolved))
                    values_arguments.append(target.as_posix())
                values = "".join(f" --values {argument}" for argument in values_arguments)
                rendered = bash_output(f"helm template {chart_name} oci://{entry}{values}")
                chart_blocks.extend(_split_documents(rendered))
        write_generated_file(
            output_path,
            output_path.read_text(encoding="utf-8") + "".join(f"---\n{block}" for block in chart_blocks),
        )

    # Sort documents by kind, namespace, and name, since score-k8s emits in randomised map order.
    documents = _split_documents(output_path.read_text(encoding="utf-8"))
    decorated: list[tuple[str, str, str, int, str]] = []
    for index, block in enumerate(documents):
        manifest = ManifestDocument.model_validate(yaml.safe_load(block) or {})
        decorated.append((manifest.kind, manifest.metadata.namespace, manifest.metadata.name, index, block))
    decorated.sort()
    write_generated_file(output_path, "".join(f"---\n{block}" for *_, block in decorated))

    # Reduce recorded workload paths to bare filenames so the state file reproduces.
    state_path = state_dir / "state.yaml"
    state_text = state_path.read_text(encoding="utf-8")
    for name in k8s.workloads:
        state_text = re.sub(rf"(?m)^(\s*file:\s*).*/{re.escape(name)}$", rf"\g<1>{name}", state_text)
    write_generated_file(state_path, state_text)

    # Rewrite each random instance_suffix to a name-derived hash so output reproduces from fresh state.
    state = ScoreState.model_validate(yaml.safe_load(state_text) or {})
    manifest_text = output_path.read_text(encoding="utf-8")
    for name, entry in state.workloads.items():
        current = entry.instance_suffix
        if not current:
            continue
        wanted = "-" + sha256(name.encode()).hexdigest()[:10]
        if current == wanted:
            continue
        state_text = state_text.replace(f"instance_suffix: {current}", f"instance_suffix: {wanted}")
        manifest_text = manifest_text.replace(f"{name}{current}", f"{name}{wanted}")
    write_generated_file(state_path, state_text)
    write_generated_file(output_path, manifest_text)

    shutil.rmtree(state_dir, ignore_errors=True)


def contains_marker(node: Any) -> bool:
    if _marker_entry(node, MARKERS) is not None:
        return True

    if is_mapping(node):
        return any(contains_marker(value) for value in node.values())

    if is_list(node):
        return any(contains_marker(item) for item in node)

    return False


def resolve_includes(node: Any, root: Path, source: str, ancestors: tuple[Path, ...] = ()) -> Any:
    entry = _marker_entry(node, INCLUDE_MARKERS)
    if entry is not None:
        key, value = entry
        if not isinstance(value, str):
            raise RuntimeError(f"{source}: {key} takes a repo-root-relative path string, not {type(value).__name__}")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"{source}: include path {value!r} must anchor at the repo root")
        path = root / relative
        if path in ancestors:
            raise RuntimeError(f"{source}: include {value!r} participates in a cycle")
        if not path.is_file():
            raise RuntimeError(f"{source}: include {value!r} does not exist at the repo root")
        text = path.read_text(encoding="utf-8")
        if key == TEXT_MARKER:
            return text
        parsed = yaml.safe_load(text)
        if parsed is None:
            raise RuntimeError(f"{source}: include {value!r} is empty")
        return resolve_includes(parsed, root, source, (*ancestors, path))

    if is_mapping(node):
        return {key: resolve_includes(value, root, source, ancestors) for key, value in node.items()}

    if is_list(node):
        return [resolve_includes(item, root, source, ancestors) for item in node]

    return node


def resolve_lock_refs(node: Any, images: dict[str, str], source: str) -> Any:
    entry = _marker_entry(node, LOCK_MARKERS)
    if entry is not None:
        marker, value = entry
        if marker == LOCK_MARKER:
            if not isinstance(value, str):
                raise RuntimeError(f"{source}: lock takes a lock-entry name string, not {type(value).__name__}")
            if value not in images:
                raise RuntimeError(f"{source}: lock entry {value!r} does not exist in the image lock")
            return images[value]

        if isinstance(value, str):
            image_spec = LockImageSpec(key=value)
        elif is_mapping(value):
            image_spec = LockImageSpec.model_validate(value)
        else:
            raise RuntimeError(
                f"{source}: lock-image takes a lock-entry name or a key/digest_field mapping, not {type(value).__name__}"
            )
        if image_spec.key not in images:
            raise RuntimeError(f"{source}: lock entry {image_spec.key!r} does not exist in the image lock")
        reference = images[image_spec.key]
        # Every lock entry is digest-pinned at load (ImageLock), so the split always yields a digest.
        path_part, _, digest = reference.rpartition("@")

        fields: dict[str, str] = {}
        tagged = path_part.rpartition(":")
        if tagged[1] and "/" not in tagged[2]:
            fields["registry"], fields["repository"] = _split_path(tagged[0], reference, source)
            fields["tag"] = tagged[2]
        else:
            fields["registry"], fields["repository"] = _split_path(path_part, reference, source)

        if image_spec.digest_field == "sha":
            # Spell the digest bare; the grafana chart template re-adds the sha256: prefix.
            fields["sha"] = digest.removeprefix("sha256:")
        else:
            fields["digest"] = digest
        return fields

    if is_mapping(node):
        image = node.get(IMAGE_FIELD)
        if isinstance(image, str) and IMAGE_HOLE_PATTERN.search(image) is None:
            raise RuntimeError(
                f"{source}: image {image!r} is a hand-written image reference; "
                "write the field as image: {lock: NAME} to consume the lock"
            )
        return {key: resolve_lock_refs(value, images, source) for key, value in node.items()}

    if is_list(node):
        return [resolve_lock_refs(item, images, source) for item in node]

    return node


def _split_path(path: str, reference: str, source: str) -> tuple[str, str]:
    registry, separator, repository = path.rpartition("/")
    if not separator or not registry or not repository:
        raise RuntimeError(f"{source}: lock reference {reference!r} has no registry/repository split")
    return registry, repository


def _marker_entry(node: Any, markers: tuple[str, ...]) -> tuple[str, Any] | None:
    if is_mapping(node) and len(node) == 1:
        key, value = next(iter(node.items()))
        if key in markers:
            return (key, value)

    return None


def collect_secret_keys(node: object, secret_name: str, consumed: set[str]) -> None:
    if is_mapping(node):
        reference = node.get("secretKeyRef")
        if is_mapping(reference) and reference.get("name") == secret_name:
            key = reference.get("key")
            if isinstance(key, str):
                consumed.add(key)

        for value in node.values():
            collect_secret_keys(value, secret_name, consumed)

    elif is_list(node):
        for item in node:
            collect_secret_keys(item, secret_name, consumed)


def _init_state(
    command: str,
    provisioners: list[Path],
    patch_templates: list[Path],
    *,
    working_dir: Path,
) -> None:
    # Init idempotently with POSIX relative paths, so no machine path or separator reaches a state file.
    flags = "".join(
        f" --provisioners {shlex.quote(Path(os.path.relpath(provisioner, working_dir)).as_posix())}"
        for provisioner in provisioners
    )
    flags += "".join(
        f" --patch-templates {shlex.quote(Path(os.path.relpath(patch_template, working_dir)).as_posix())}"
        for patch_template in patch_templates
    )
    bash(f"{command}{flags}", cwd=working_dir)


@contextmanager
def _rendered_workloads(
    config: DockerDevkitConfig,
    workloads: list[str],
    rendering_vars: dict[str, str],
    images: dict[str, str],
) -> Generator[str]:
    # Expand ${ALL_CAPS} placeholders into tempdir copies, since Score documents cannot read the
    # environment. Holes fill from the rendering's vars map first (bundle vars for compose,
    # k8s.vars for k8s), then the environment (the tree-tag SHAs); ${VAR:?err}/${VAR:-}
    # runtime holes never match the bare-placeholder pattern, so they ride through to compose
    # interpolation untouched. Lock markers resolve first and structurally — a workload's
    # image field is metadata score never substitutes outputs into, so a third-party image
    # rides as {lock: NAME} and lands digest-pinned in both renderings.
    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)

        for name in workloads:
            source_path = config.score_dir_path() / name
            document = resolve_lock_refs(_load_yaml_file(source_path), images, f"workload {name}")
            source = yaml.safe_dump(document, sort_keys=False, width=1000)
            rendered = PLACEHOLDER.sub(partial(_placeholder_value, rendering_vars, name), source)
            (directory / name).write_text(rendered, "utf-8")

        yield " ".join((directory / name).as_posix() for name in workloads)


def _placeholder_value(rendering_vars: dict[str, str], workload_name: str, match: re.Match[str]) -> str:
    variable = match.group(1)
    value = rendering_vars.get(variable) or os.environ.get(variable)
    if not value:
        raise SystemExit(
            f"{workload_name}: ${{{variable}}} is set in neither the rendering's vars map nor the "
            "environment (service SHAs derive from the build keys)"
        )
    return value


def dump_compose(value: object) -> str:
    return yaml.dump(
        value,
        Dumper=ComposeDumper,
        default_flow_style=False,
        sort_keys=False,
        width=1_000_000,
        allow_unicode=True,
    )


def write_generated_file(path: Path, text: str) -> None:
    # Write with newline="" so a Windows run does not rewrite line endings as CRLF.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)

    # Normalise to 0644, since score-k8s writes its state file executable.
    path.chmod(0o644)


def _load_yaml_file(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _split_documents(text: str) -> list[str]:
    return [block for block in re.split(r"(?m)^---$\n", text) if block.strip()]
