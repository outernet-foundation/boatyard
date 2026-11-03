from __future__ import annotations

import os
import re
import shlex
from contextlib import ExitStack
from functools import partial
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, NamedTuple, Self

import yaml
from bashrun.bash import bash
from pydantic import (
    BaseModel,
    ConfigDict,
    RootModel,
    ValidationInfo,
    field_validator,
    model_validator,
)

from boatyard.config import (
    ENV_NAME_PATTERN,
    PLACEHOLDER,
    Yaml,
    is_list,
    is_mapping,
    load_yaml_file,
    Config,
    WorkloadDocument,
)

PATCH_TEMPLATES_DIR = "patch_templates"

SECRETS_FILE = Path("stack/secrets.yaml")
SECRETS_OUTPUT = Path("stack/generated/secrets.txt")

STRING_FORM_FIELDS = ("state", "outputs", "volumes", "services", "manifests")

TEXT_MARKER = "include"
YAML_MARKER = "include-yaml"
LOCK_MARKER = "lock"
LOCK_IMAGE_MARKER = "lock-image"
LOCK_MARKERS = (LOCK_MARKER, LOCK_IMAGE_MARKER)
Substrate = Literal["compose", "k8s"]
IMAGE_FIELD = "image"
IMAGE_HOLE_PATTERN = re.compile(r"\$\{|{{")


class SecretsDeclaration(RootModel[dict[str, "SecretKey"]]):
    @model_validator(mode="before")
    @classmethod
    def _coerce_string_specs(cls, value: Yaml) -> Yaml:
        if value is None:
            value = {}
        if not is_mapping(value):
            raise ValueError("must be a KEY: description mapping")
        return {name: ({"description": spec} if isinstance(spec, str) else spec) for name, spec in value.items()}

    @model_validator(mode="after")
    def _require_all_caps_keys(self) -> Self:
        for name in self.root:
            if not ENV_NAME_PATTERN.match(name):
                raise ValueError(f"{name!r} must be an ALL_CAPS key")
        return self

    def validate_consumed(self, consumed: set[str], rendering: Substrate, source: str) -> None:
        undeclared = sorted(consumed - self.root.keys())
        if undeclared:
            raise ValueError(f"{source}: the {rendering} rendering consumes undeclared keys: " + ", ".join(undeclared))


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
    digest_field: Literal["digest", "sha"] = "digest"


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


class LiteralBlockDumper(yaml.SafeDumper):
    pass


class LoadedWorkload(NamedTuple):
    file_name: str
    raw: Yaml
    document: WorkloadDocument


class MarkerContext(NamedTuple):
    root: Path
    images: dict[str, str]
    source: str
    ancestors: tuple[Path, ...] = ()


class ImageReference(NamedTuple):
    registry: str
    repository: str
    tag: str | None
    digest: str


def _represent_multiline(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    return yaml.ScalarNode("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


LiteralBlockDumper.add_representer(str, _represent_multiline)


def render_score(
    config: Config,
    substrate: Substrate,
    workloads: list[LoadedWorkload],
    rendering_vars: dict[str, str],
    templates: list[str],
    output: Path,
    images: dict[str, str],
    service_shas: dict[str, str],
) -> None:
    # Derive the per-substrate names and create the output parent.
    root = config.root
    score_dir = root / config.score_dir
    tool = f"score-{substrate}"
    rendering = substrate
    # Both tools write their state dir into the cwd; run them in the output directory.
    working_dir = output.parent
    project_name = None if substrate == "k8s" else config.project_name
    output.parent.mkdir(parents=True, exist_ok=True)

    with (
        as_file(files("boatyard") / PATCH_TEMPLATES_DIR) as builtin_templates,
        ExitStack() as stack,
    ):
        # Collect the resource types the workloads reference.
        referenced: set[str] = set()
        for workload in workloads:
            resources = workload.raw.get("resources") if is_mapping(workload.raw) else None
            if is_mapping(resources):
                for resource in resources.values():
                    resource_type = resource.get("type") if is_mapping(resource) else None
                    if isinstance(resource_type, str):
                        referenced.add(resource_type)

        # Select the rendering's provisioner files that declare a referenced type.
        selected: list[tuple[str, list[Yaml]]] = []
        rendering_dir = score_dir / rendering
        if rendering_dir.is_dir():
            for path in sorted(rendering_dir.glob("*.yaml")):
                entries = load_yaml_file(path)
                if is_list(entries) and any(is_mapping(entry) and entry.get("type") in referenced for entry in entries):
                    selected.append((f"{rendering}/{path.name}", entries))

        # Compile the selected provisioners into a per-run tempdir under score_dir.
        paths: list[Path] = []
        if selected:
            directory = Path(stack.enter_context(TemporaryDirectory(prefix="compiled-provisioners-", dir=score_dir)))
            for name, entries in selected:
                context = MarkerContext(root=root, images=images, source=name)
                compiled_entries: list[Yaml] = []
                for entry in entries:
                    resolved = resolve_markers(entry, context)
                    AuthoredProvisioner.model_validate(resolved)
                    if not is_mapping(resolved):
                        continue
                    compiled_entries.append({
                        key: dump_yaml(value) if key in STRING_FORM_FIELDS and value is not None else value
                        for key, value in resolved.items()
                    })
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                write_file(target, dump_yaml(compiled_entries))
                paths.append(target)

        # Build the tool flags and run init.
        flags = "".join(
            f" --provisioners {shlex.quote(Path(os.path.relpath(path, working_dir)).as_posix())}" for path in paths
        )
        flags += "".join(
            f" --patch-templates {shlex.quote(Path(os.path.relpath(builtin_templates / name, working_dir)).as_posix())}"
            for name in templates
        )
        project = f" --project {project_name}" if project_name is not None else ""
        bash(f"{tool} init{project} --no-sample{flags}", cwd=working_dir)

        # Pre-seed each workload's instance suffix so generated names are deterministic.
        if substrate == "k8s":
            state_path = state_dir(tool, output) / "state.yaml"
            state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
            if not is_mapping(state):
                state = {}
            entries = state.get("workloads")
            if not is_mapping(entries):
                entries = {}
                state["workloads"] = entries
            for workload in workloads:
                workload_name = Path(workload.file_name).stem
                entry = entries.get(workload_name)
                if not is_mapping(entry):
                    entry = {}
                    entries[workload_name] = entry
                entry["instance_suffix"] = "-" + sha256(workload_name.encode()).hexdigest()[:10]
            write_file(state_path, dump_yaml(state))

        # Render the expanded workload copies and run generate.
        copies_directory = Path(stack.enter_context(TemporaryDirectory()))
        for workload in workloads:
            expanded = PLACEHOLDER.sub(
                partial(_placeholder_value, rendering_vars, service_shas, images, workload.file_name),
                dump_yaml(workload.raw),
            )
            (copies_directory / workload.file_name).write_text(expanded, "utf-8")
        rendered = " ".join((copies_directory / workload.file_name).as_posix() for workload in workloads)
        bash(f"{tool} generate {rendered} --output {output.as_posix()}", cwd=working_dir)


def load_workloads(score_dir: Path, names: list[str], lens: type[WorkloadDocument]) -> list[LoadedWorkload]:
    loaded: list[LoadedWorkload] = []
    for name in names:
        raw = load_yaml_file(score_dir / name)
        loaded.append(LoadedWorkload(name, raw, lens.parse(raw, name)))
    return loaded


def state_dir(tool: str, output: Path) -> Path:
    return output.parent / state_dir_name(tool)


def state_dir_name(tool: str) -> str:
    return f".{tool}"


def resolve_markers(node: Yaml, context: MarkerContext) -> Yaml:
    # Includes splice in structure that may itself carry lock markers, so includes resolve first.
    return resolve_locks(expand_includes(node, context), context)


def expand_includes(node: Yaml, context: MarkerContext) -> Yaml:
    match node:
        case list():
            # Resolve every element of a list.
            expanded = [expand_includes(item, context) for item in node]
        case dict():
            match tuple(node.items()):
                # A single-key include marker splices in file content.
                case [(marker, value)] if marker == TEXT_MARKER:
                    expanded = include_text(value, context)
                case [(marker, value)] if marker == YAML_MARKER:
                    expanded = include_yaml(value, context)
                # A lock marker owns its value whole; the lock pass consumes or refuses it.
                case [(marker, _)] if marker in LOCK_MARKERS:
                    expanded = node
                # Any other mapping resolves child by child.
                case entries:
                    expanded = {key: expand_includes(child, context) for key, child in entries}
        case _:
            # Scalars pass through untouched.
            expanded = node

    return expanded


def include_text(value: Yaml, context: MarkerContext) -> str:
    path = included_path(value, TEXT_MARKER, context)
    return path.read_text(encoding="utf-8")


def include_yaml(value: Yaml, context: MarkerContext) -> Yaml:
    path = included_path(value, YAML_MARKER, context)

    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"{context.source}: include {value!r} is not valid YAML: {error}") from error

    if parsed is None:
        raise ValueError(f"{context.source}: include {value!r} is empty")

    return expand_includes(parsed, context._replace(ancestors=(*context.ancestors, path)))


def included_path(value: Yaml, marker: str, context: MarkerContext) -> Path:
    source = context.source

    match value:
        case str():
            relative = Path(value)
        case _:
            raise ValueError(f"{source}: {marker} takes a repo-root-relative path string, not {type(value).__name__}")

    # Anchor include paths at the repo root.
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{source}: include path {value!r} must anchor at the repo root")

    path = context.root / relative

    # Refuse cycles and missing targets.
    if path in context.ancestors:
        raise ValueError(f"{source}: include {value!r} participates in a cycle")
    if not path.is_file():
        raise ValueError(f"{source}: include {value!r} does not exist at the repo root")

    return path


def resolve_locks(node: Yaml, context: MarkerContext) -> Yaml:
    match node:
        case list():
            # Resolve every element of a list.
            resolved = [resolve_locks(item, context) for item in node]
        case dict():
            match tuple(node.items()):
                # A single-key lock marker substitutes the locked reference as one string.
                case [(marker, value)] if marker == LOCK_MARKER:
                    resolved = locked_reference(value, context)
                case [(marker, value)] if marker == LOCK_IMAGE_MARKER:
                    resolved = lock_image_fields(value, context.images, context.source)
                # A plain mapping refuses literal images, then resolves child by child.
                case entries:
                    _check_literal_image(node, context.source)
                    resolved = {key: resolve_locks(child, context) for key, child in entries}
        case _:
            # Scalars pass through untouched.
            resolved = node

    return resolved


def locked_reference(value: Yaml, context: MarkerContext) -> str:
    match value:
        case str() if value in context.images:
            reference = context.images[value]
        case str():
            raise ValueError(f"{context.source}: lock entry {value!r} does not exist in the image lock")
        case _:
            raise ValueError(f"{context.source}: lock takes a lock-entry name string, not {type(value).__name__}")

    return reference


def lock_image_fields(value: Yaml, images: dict[str, str], source: str) -> dict[str, Yaml]:
    # A bare string names the lock entry; a mapping spells key and digest_field.
    if isinstance(value, str):
        image_spec = LockImageSpec(key=value)
    elif is_mapping(value):
        image_spec = LockImageSpec.model_validate(value)
    else:
        raise ValueError(
            f"{source}: lock-image takes a lock-entry name or a key/digest_field mapping, not {type(value).__name__}"
        )

    if image_spec.key not in images:
        raise ValueError(f"{source}: lock entry {image_spec.key!r} does not exist in the image lock")

    reference = split_image_reference(images[image_spec.key], source)

    # Assemble the chart fields in the chart-convention key order, under the digest spelling.
    fields: dict[str, Yaml] = {"registry": reference.registry, "repository": reference.repository}
    if reference.tag is not None:
        fields["tag"] = reference.tag

    digest = reference.digest
    fields[image_spec.digest_field] = digest.removeprefix("sha256:") if image_spec.digest_field == "sha" else digest

    return fields


def _check_literal_image(node: dict[str, Yaml], source: str) -> None:
    image = node.get(IMAGE_FIELD)
    if isinstance(image, str) and IMAGE_HOLE_PATTERN.search(image) is None:
        raise ValueError(f"{source}: image {image!r} must consume the lock (lock: NAME / lock-image: NAME)")


def split_image_reference(reference: str, source: str) -> ImageReference:
    # Peel the trailing digest.
    path_part, _, digest = reference.rpartition("@")

    # A colon starts a tag only when no slash follows it, so a registry port stays part of the registry.
    tagged = path_part.rpartition(":")
    tag: str | None = None
    registry_part = path_part
    if tagged[1] and "/" not in tagged[2]:
        registry_part, tag = tagged[0], tagged[2]

    # The last slash splits a multi-segment registry from the repository.
    registry, separator, repository = registry_part.rpartition("/")
    if not separator or not registry or not repository:
        raise ValueError(f"{source}: lock reference {reference!r} has no registry/repository split")

    return ImageReference(registry, repository, tag, digest)


def _placeholder_value(
    rendering_vars: dict[str, str],
    service_shas: dict[str, str],
    images: dict[str, str],
    workload_name: str,
    match: re.Match[str],
) -> str:
    variable = match.group(1)
    value = rendering_vars.get(variable) or service_shas.get(variable) or images.get(variable)
    if not value:
        raise ValueError(
            f"{workload_name}: ${{{variable}}} is set in none of the rendering vars, the service shas, or the image lock"
        )
    return value


def dump_yaml(value: Yaml) -> str:
    return yaml.dump(
        value,
        Dumper=LiteralBlockDumper,
        default_flow_style=False,
        sort_keys=False,
        width=1_000_000,
        allow_unicode=True,
    )


def write_file(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)

    path.chmod(0o644)


def split_documents(text: str) -> list[str]:
    return [block for block in re.split(r"(?m)^---$\n", text) if block.strip()]
