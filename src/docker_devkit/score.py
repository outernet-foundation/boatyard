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
from typing import Literal, NamedTuple, Self

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

from docker_devkit import dockerfiles
from docker_devkit.compose import merged_environment
from docker_devkit.config import (
    COMPOSE_HOLE,
    ComposeBundle,
    CONSUMER_INTERPOLATED_ANNOTATION,
    DEV_TOGGLES_ANNOTATION,
    EDGE_SERVICE_NAME,
    ENV_NAME_PATTERN,
    K8sConfig,
    PLACEHOLDER,
    PROBE_ADAPTER_ANNOTATION,
    Target,
    WorkloadDocument,
    Yaml,
    double_dollars,
    is_list,
    is_mapping,
    Config,
    chart_lock_key,
    validate,
)
from docker_devkit.config_lock import LOCK_PATH, ConfigLock, parse_config_lock
from docker_devkit.lock import compose_env_entries, env_entries


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


STATE_PREFIX = ".score-compose/"

# Match doubled {workload}-{container} service names.
DOUBLED_SERVICE_NAME = re.compile(r"^(.+)-\1$")

PATCH_TEMPLATES_DIR = "patch_templates"
GATE_PIN_TEMPLATE = "wait-for-resources-image.tpl"
GATE_SERVICE = "wait-for-resources"
HEALTHCHECK_TEMPLATE = "probe-adapter-healthchecks.tpl"
RESTART_TEMPLATE = "restart.tpl"
GPU_POD_TEMPLATE = "gpu-pod.tpl"
SCALE_TO_ZERO_TEMPLATE = "scale-to-zero.tpl"
GPU_COMPOSE_TEMPLATES = {"cuda": "gpu-cuda.tpl", "rocm": "gpu-rocm.tpl"}

STORAGE_CLASS_VAR = "SCORE_STORAGE_CLASS"
CLOUD_STORAGE_CLASS = "hcloud-volumes"
LOCAL_STORAGE_CLASS = "local-path"

SECRETS_FILE = Path("stack/secrets.yaml")
SECRETS_OUTPUT = Path("stack/generated/secrets.txt")

STRING_FORM_FIELDS = ("state", "outputs", "volumes", "services", "manifests")

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


class SecretsDeclaration(RootModel[dict[str, "SecretKey"]]):
    @model_validator(mode="before")
    @classmethod
    def _coerce_string_specs(cls, value: Yaml) -> Yaml:
        if not is_mapping(value) or not value:
            raise ValueError("must be a non-empty KEY: description mapping")
        return {name: ({"description": spec} if isinstance(spec, str) else spec) for name, spec in value.items()}

    @model_validator(mode="after")
    def _require_all_caps_keys(self) -> Self:
        for name in self.root:
            if not ENV_NAME_PATTERN.match(name):
                raise ValueError(f"{name!r} must be an ALL_CAPS key")
        return self

    def validate_consumed(self, consumed: set[str], rendering: str, source: str) -> None:
        undeclared = sorted(consumed - self.root.keys())
        if undeclared:
            raise RuntimeError(
                f"{source}: the {rendering} rendering consumes undeclared keys: " + ", ".join(undeclared)
            )


class SecretKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    mint: bool = False

    @field_validator("description")
    @classmethod
    def _require_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must be non-empty")
        return value


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
    state: dict[str, Yaml] | None = None
    outputs: dict[str, Yaml] | None = None
    volumes: dict[str, Yaml] | None = None
    services: dict[str, Yaml] | None = None
    manifests: list[Yaml] | None = None

    @field_validator(*STRING_FORM_FIELDS, mode="before")
    @classmethod
    def _reject_string_form(cls, value: Yaml, info: ValidationInfo) -> Yaml:
        if not isinstance(value, str):
            return value

        shape = "list" if info.field_name == "manifests" else "mapping"
        raise ValueError(f"{info.field_name} is in score's compiled string form; a {shape} is required")


class CompiledProvisioners(NamedTuple):
    paths: list[Path]
    declares_services: bool


class ComposeDumper(yaml.SafeDumper):
    pass


def _represent_multiline(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # Force literal block style for multiline strings.
    return yaml.ScalarNode("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


ComposeDumper.add_representer(str, _represent_multiline)


def rendering_targets(config: Config, target: Target) -> list[Literal["compose", "k8s"]]:
    # The one fan-out decider: which renderings a run produces.
    renderings: list[Literal["compose", "k8s"]] = []
    if target in ("both", "compose"):
        renderings.append("compose")
    if target in ("both", "k8s") and config.k8s is not None:
        renderings.append("k8s")
    return renderings


def generate_artifacts(
    config: Config, target: Target, local: bool, tool_environment: dict[str, str] | None = None
) -> list[Path]:
    # Require the build keys, derive the tree tags, and load the lock.
    if not (config.repository or config.base_images):
        raise RuntimeError("repository or base_images is required in docker-devkit.yaml")
    root = config.root
    service_shas = dockerfiles.compute_service_shas(root, config.service_builds)
    lock_path = root / LOCK_PATH
    if not lock_path.is_file():
        raise RuntimeError(f"{lock_path.as_posix()}: the image lock is required")
    lock = parse_config_lock(lock_path)
    if not lock.images and not lock.charts:
        raise RuntimeError(f"{lock_path.as_posix()}: the image lock carries no entries")
    k8s_output = root / (config.k8s_local_output if local else config.k8s_output)

    outputs: list[Path] = []
    renderings = rendering_targets(config, target)

    if "compose" in renderings:
        generate_compose(
            root, config, compose_env_entries(lock, config, merged_environment(root)), service_shas, tool_environment
        )
        outputs.extend(bundle.output for bundle in config.compose_bundles())

    if "k8s" in renderings and config.k8s is not None:
        storage_class = LOCAL_STORAGE_CLASS if local else CLOUD_STORAGE_CLASS
        generate_k8s(root, config, config.k8s, k8s_output, lock, service_shas, storage_class, tool_environment)
        outputs.append(k8s_output)

    if (root / SECRETS_FILE).is_file():
        # Validate the consumed keys and write the mint list.
        declaration_path = root / SECRETS_FILE
        k8s_secret = f"{config.project_name}-secrets"

        declaration = validate(SecretsDeclaration, _load_yaml_file(declaration_path), declaration_path.as_posix())

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


def generate_compose(
    root: Path,
    config: Config,
    images: dict[str, str],
    service_shas: dict[str, str],
    tool_environment: dict[str, str] | None,
) -> None:
    bundles = config.compose_bundles()
    if not bundles:
        raise RuntimeError("the compose target requires local service entries (workloads:)")
    score_dir = config.root / config.score_dir
    score_state = score_dir / STATE_PREFIX

    with _builtin_patch_templates() as builtin_templates:
        for bundle in bundles:
            interpolated_targets: set[str] = set()
            toggle_services: dict[str, str] = {}
            for name in bundle.workloads:
                workload = validate(WorkloadDocument, _load_yaml_file(score_dir / name), name)
                annotations = workload.metadata.annotations

                consumer_interpolated = annotations.get(CONSUMER_INTERPOLATED_ANNOTATION)
                if consumer_interpolated is not None:
                    interpolated_targets.update(consumer_interpolated.split(","))

                # The httpGet requirement is a compose placement law: score-compose drops httpGet probes.
                if annotations.get(PROBE_ADAPTER_ANNOTATION) is None:
                    for probe in workload.http_get_probes:
                        raise SystemExit(f"{name}: a {probe} httpGet requires {PROBE_ADAPTER_ANNOTATION}")

                toggle_raw, service_name = workload.dev_toggles
                if toggle_raw is not None and service_name is not None:
                    if service_name in toggle_services:
                        raise SystemExit(
                            f"{toggle_services[service_name]} and {name} both declare "
                            f"{DEV_TOGGLES_ANNOTATION} for {service_name}"
                        )
                    toggle_services[service_name] = name
            provisioner_names = closure_provisioners(score_dir, "compose", bundle.workloads)
            with compiled_provisioners(score_dir, provisioner_names, root, images) as provisioners:
                if provisioners.declares_services and "ALPINE_IMAGE" not in images:
                    raise RuntimeError(
                        f"bundle {bundle.name!r}: its provisioners declare services; "
                        f"{LOCK_PATH.as_posix()} must carry the ALPINE_IMAGE entry"
                    )
                templates = _compose_templates(builtin_templates, bundle, provisioners.declares_services)
                shutil.rmtree(score_state, ignore_errors=True)
                _init_state(
                    f"score-compose init --project {config.project_name} --no-sample",
                    provisioners.paths,
                    templates,
                    working_dir=score_dir,
                    environment=tool_environment,
                )

                output = bundle.output
                output.parent.mkdir(parents=True, exist_ok=True)
                with _rendered_workloads(config, bundle.workloads, bundle.vars, images, service_shas) as workloads:
                    bash(
                        f"score-compose generate {workloads} --output {output.as_posix()}",
                        cwd=score_dir,
                        env=tool_environment,
                    )

                # Post-passes run as typed transformations; the model dumps exactly once.
                document = validate(
                    ComposeDocument, yaml.safe_load(output.read_text(encoding="utf-8")), output.as_posix()
                )
                services = document.services or {}

                # Collapse doubled {workload}-{container} service names.
                collapsed: dict[str, ComposeService] = {}
                for name, service in services.items():
                    match = DOUBLED_SERVICE_NAME.fullmatch(name)
                    target = match.group(1) if match is not None else name
                    if target != name and target in services:
                        raise SystemExit(f"service rename {name} -> {target} collides with an existing service")
                    collapsed[target] = service

                # Rename the gate per entry and follow the depends_on references.
                gate = f"{GATE_SERVICE}-{bundle.name}"
                if GATE_SERVICE in collapsed:
                    if gate in collapsed:
                        raise SystemExit(f"gate rename {GATE_SERVICE} -> {gate} collides with an existing service")
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
                    raise SystemExit(
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


@contextmanager
def _builtin_patch_templates() -> Generator[Path]:
    with as_file(files("docker_devkit") / PATCH_TEMPLATES_DIR) as directory:
        yield directory


def _compose_templates(builtin_templates: Path, bundle: ComposeBundle, declares_services: bool) -> list[Path]:
    names = [HEALTHCHECK_TEMPLATE, RESTART_TEMPLATE]
    if declares_services:
        names.append(GATE_PIN_TEMPLATE)
    gpu_template = GPU_COMPOSE_TEMPLATES.get(bundle.vars.get("GPU_VARIANT", ""))
    if gpu_template is not None:
        names.append(gpu_template)
    return [builtin_templates / name for name in names]


def closure_provisioners(score_dir: Path, rendering: str, workloads: list[str]) -> list[str]:
    # Select the provisioner files whose resource types the workloads reference.
    referenced: set[str] = set()
    for name in workloads:
        document = _load_yaml_file(score_dir / name)
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
        entries = _load_yaml_file(path)
        if not is_list(entries):
            continue
        if any(is_mapping(entry) and entry.get("type") in referenced for entry in entries):
            selected.append(f"{rendering}/{path.name}")
    return selected


@contextmanager
def compiled_provisioners(
    source_dir: Path, names: list[str], root: Path, images: dict[str, str]
) -> Generator[CompiledProvisioners]:
    # Compile into a per-run tempdir under source_dir.
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
                raise RuntimeError(f"{name}: an authored provisioner file must be a non-empty YAML list")
            compiled_entries: list[Yaml] = []
            for entry in document:
                validate(AuthoredProvisioner, entry, name)
                if not is_mapping(entry):
                    continue
                if entry.get("services"):
                    declares_services = True

                # Compile each entry in authored key order.
                compiled: dict[str, Yaml] = {}
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


def generate_k8s(
    root: Path,
    config: Config,
    k8s: K8sConfig,
    output_path: Path,
    lock: ConfigLock,
    service_shas: dict[str, str],
    storage_class: str,
    tool_environment: dict[str, str] | None,
) -> None:
    # Run score-k8s in the output's directory, where it writes its .score-k8s state.
    images = env_entries(lock)
    state_dir = output_path.parent / ".score-k8s"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    score_dir = root / config.score_dir
    rendering_vars = {**k8s.vars, STORAGE_CLASS_VAR: storage_class}

    compose_workloads = {name for bundle in config.compose_bundles() for name in bundle.workloads}
    for name in k8s.workloads:
        if name in compose_workloads:
            continue
        k8s_annotations = validate(WorkloadDocument, _load_yaml_file(score_dir / name), name).metadata.annotations
        if DEV_TOGGLES_ANNOTATION in k8s_annotations:
            raise SystemExit(
                f"{name}: {DEV_TOGGLES_ANNOTATION} is compose dev-overlay vocabulary; this workload renders k8s-only"
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
            environment=tool_environment,
        )

        _seed_instance_suffixes(state_dir / "state.yaml", score_dir, k8s.workloads)

        with _rendered_workloads(config, k8s.workloads, rendering_vars, images, service_shas) as workloads:
            bash(
                f"score-k8s generate {workloads} --output {output_path.as_posix()}",
                cwd=output_path.parent,
                env=tool_environment,
            )

    # Render the charts through helm template.
    if k8s.charts:
        chart_blocks: list[str] = []
        with TemporaryDirectory(prefix="resolved-values-") as values_directory:
            values_dir = Path(values_directory)
            for chart_name in k8s.charts:
                key = chart_lock_key(chart_name)
                entry = lock.require(key, f"chart {chart_name!r}")
                # Pass a marker-free values file through untouched; resolve markers into a tempdir copy.
                values_arguments: list[str] = []
                path = root / config.score_dir / "k8s" / "values" / f"{chart_name}.yaml"
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
                rendered = bash_output(f"helm template {chart_name} oci://{entry}{values}", env=tool_environment)
                chart_blocks.extend(_split_documents(rendered))
        write_generated_file(
            output_path,
            output_path.read_text(encoding="utf-8") + "".join(f"---\n{block}" for block in chart_blocks),
        )

    # Sort documents by kind, namespace, and name.
    documents = _split_documents(output_path.read_text(encoding="utf-8"))
    decorated: list[tuple[str, str, str, int, str]] = []
    for index, block in enumerate(documents):
        manifest = ManifestDocument.model_validate(yaml.safe_load(block) or {})
        decorated.append((manifest.kind, manifest.metadata.namespace, manifest.metadata.name, index, block))
    decorated.sort()
    write_generated_file(output_path, "".join(f"---\n{block}" for *_, block in decorated))

    shutil.rmtree(state_dir, ignore_errors=True)


def contains_marker(node: Yaml) -> bool:
    if _marker_entry(node, MARKERS) is not None:
        return True

    if is_mapping(node):
        return any(contains_marker(value) for value in node.values())

    if is_list(node):
        return any(contains_marker(item) for item in node)

    return False


def resolve_includes(node: Yaml, root: Path, source: str, ancestors: tuple[Path, ...] = ()) -> Yaml:
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
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ValueError(f"{source}: include {value!r} is not valid YAML: {error}") from error
        if parsed is None:
            raise RuntimeError(f"{source}: include {value!r} is empty")
        return resolve_includes(parsed, root, source, (*ancestors, path))

    if is_mapping(node):
        return {key: resolve_includes(value, root, source, ancestors) for key, value in node.items()}

    if is_list(node):
        return [resolve_includes(item, root, source, ancestors) for item in node]

    return node


def resolve_lock_refs(node: Yaml, images: dict[str, str], source: str) -> Yaml:
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
        path_part, _, digest = reference.rpartition("@")

        fields: dict[str, Yaml] = {}
        tagged = path_part.rpartition(":")
        if tagged[1] and "/" not in tagged[2]:
            fields["registry"], fields["repository"] = _split_path(tagged[0], reference, source)
            fields["tag"] = tagged[2]
        else:
            fields["registry"], fields["repository"] = _split_path(path_part, reference, source)

        if image_spec.digest_field == "sha":
            # Spell the digest bare.
            fields["sha"] = digest.removeprefix("sha256:")
        else:
            fields["digest"] = digest
        return fields

    if is_mapping(node):
        image = node.get(IMAGE_FIELD)
        if isinstance(image, str) and IMAGE_HOLE_PATTERN.search(image) is None:
            raise RuntimeError(f"{source}: image {image!r} must consume the lock (lock: NAME / lock-image: NAME)")
        return {key: resolve_lock_refs(value, images, source) for key, value in node.items()}

    if is_list(node):
        return [resolve_lock_refs(item, images, source) for item in node]

    return node


def _split_path(path: str, reference: str, source: str) -> tuple[str, str]:
    registry, separator, repository = path.rpartition("/")
    if not separator or not registry or not repository:
        raise RuntimeError(f"{source}: lock reference {reference!r} has no registry/repository split")
    return registry, repository


def _marker_entry(node: Yaml, markers: tuple[str, ...]) -> tuple[str, Yaml] | None:
    if is_mapping(node) and len(node) == 1:
        key, value = next(iter(node.items()))
        if key in markers:
            return (key, value)

    return None


def collect_secret_keys(node: Yaml, secret_name: str, consumed: set[str]) -> None:
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
    environment: dict[str, str] | None,
) -> None:
    # Init idempotently with POSIX-relative paths.
    flags = "".join(
        f" --provisioners {shlex.quote(Path(os.path.relpath(provisioner, working_dir)).as_posix())}"
        for provisioner in provisioners
    )
    flags += "".join(
        f" --patch-templates {shlex.quote(Path(os.path.relpath(patch_template, working_dir)).as_posix())}"
        for patch_template in patch_templates
    )
    bash(f"{command}{flags}", cwd=working_dir, env=environment)


def _seed_instance_suffixes(state_path: Path, score_dir: Path, workloads: list[str]) -> None:
    # Pre-seed each workload's instance suffix so the tool names instances
    # deterministically by construction; init's other state keys ride through.
    state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    if not is_mapping(state):
        state = {}
    entries = state.get("workloads")
    if not is_mapping(entries):
        entries = {}
        state["workloads"] = entries
    for name in workloads:
        workload_name = validate(WorkloadDocument, _load_yaml_file(score_dir / name), name).metadata.name
        if not workload_name:
            continue
        entry = entries.get(workload_name)
        if not is_mapping(entry):
            entry = {}
            entries[workload_name] = entry
        entry["instance_suffix"] = "-" + sha256(workload_name.encode()).hexdigest()[:10]
    write_generated_file(state_path, dump_compose(state))


@contextmanager
def _rendered_workloads(
    config: Config,
    workloads: list[str],
    rendering_vars: dict[str, str],
    images: dict[str, str],
    service_shas: dict[str, str],
) -> Generator[str]:
    # Expand ${ALL_CAPS} placeholders into tempdir copies.
    with TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)

        for name in workloads:
            source_path = config.root / config.score_dir / name
            document = resolve_lock_refs(_load_yaml_file(source_path), images, f"workload {name}")
            source = dump_compose(document)
            rendered = PLACEHOLDER.sub(partial(_placeholder_value, rendering_vars, service_shas, name), source)
            (directory / name).write_text(rendered, "utf-8")

        yield " ".join((directory / name).as_posix() for name in workloads)


def _placeholder_value(
    rendering_vars: dict[str, str], service_shas: dict[str, str], workload_name: str, match: re.Match[str]
) -> str:
    variable = match.group(1)
    value = rendering_vars.get(variable) or service_shas.get(variable)
    if not value:
        raise SystemExit(
            f"{workload_name}: ${{{variable}}} is set in neither the rendering's vars map nor the derived service shas"
        )
    return value


def dump_compose(value: Yaml) -> str:
    return yaml.dump(
        value,
        Dumper=ComposeDumper,
        default_flow_style=False,
        sort_keys=False,
        width=1_000_000,
        allow_unicode=True,
    )


def write_generated_file(path: Path, text: str) -> None:
    # Write with newline="" to prevent CRLF rewrites.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)

    # Normalise the mode to 0644.
    path.chmod(0o644)


def _load_yaml_file(path: Path) -> Yaml:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"{path.as_posix()}: {error}") from error


def _split_documents(text: str) -> list[str]:
    return [block for block in re.split(r"(?m)^---$\n", text) if block.strip()]
