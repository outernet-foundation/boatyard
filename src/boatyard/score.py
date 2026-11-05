from __future__ import annotations

import os
import re
import shlex
from contextlib import ExitStack
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
    Field,
    RootModel,
    ValidationInfo,
    field_validator,
    model_validator,
)

from boatyard.config_file import (
    ENV_NAME_PATTERN,
    PLACEHOLDER,
    Yaml,
    is_list,
    is_mapping,
    load_yaml_file,
    Config,
    WorkloadDocument,
)
from boatyard.markers import MarkerContext, resolve_markers

PATCH_TEMPLATES_DIR = "patch_templates"

SECRETS_FILE = Path("stack/secrets.yaml")
SECRETS_OUTPUT = Path("stack/generated/secrets.txt")

STRING_FORM_FIELDS = ("state", "outputs", "volumes", "services", "manifests")

Substrate = Literal["compose", "k8s"]


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


class ToolState(BaseModel):
    # Type only the workloads shape of the tool-owned state file
    model_config = ConfigDict(extra="allow")

    workloads: dict[str, dict[str, Yaml]] = Field(default_factory=dict)


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
    # Derive the per-substrate names and create the output parent
    root = config.root
    score_dir = root / config.score_dir
    tool = f"score-{substrate}"
    rendering = substrate
    # Run both tools in the output directory because both write their state dir into the cwd
    working_dir = output.parent
    project_name = None if substrate == "k8s" else config.project_name
    output.parent.mkdir(parents=True, exist_ok=True)

    with (
        as_file(files("boatyard") / PATCH_TEMPLATES_DIR) as builtin_templates,
        ExitStack() as stack,
    ):
        # Collect the resource types the workloads reference
        referenced = {resource.type for workload in workloads for resource in workload.document.resources.values()}

        # Select the rendering's provisioner files that declare a referenced type
        selected: list[tuple[str, list[Yaml]]] = []
        rendering_dir = score_dir / rendering
        if rendering_dir.is_dir():
            for path in sorted(rendering_dir.glob("*.yaml")):
                entries = load_yaml_file(path)
                if is_list(entries) and any(is_mapping(entry) and entry.get("type") in referenced for entry in entries):
                    selected.append((f"{rendering}/{path.name}", entries))

        # Compile the selected provisioners into a per-run tempdir under score_dir
        paths: list[Path] = []
        directory = Path(stack.enter_context(TemporaryDirectory(prefix="compiled-provisioners-", dir=score_dir)))
        for name, entries in selected:
            context = MarkerContext(root=root, images=images, source=name)
            compiled_entries: list[Yaml] = []
            for entry in entries:
                resolved = resolve_markers(entry, context)
                AuthoredProvisioner.model_validate(resolved)
                if is_mapping(resolved):
                    compiled_entries.append({
                        key: dump_yaml(value) if key in STRING_FORM_FIELDS and value is not None else value
                        for key, value in resolved.items()
                    })
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            write_file(target, dump_yaml(compiled_entries))
            paths.append(target)

        # Build the tool flags and run init
        provisioner_flags = "".join(
            f" --provisioners {shlex.quote(Path(os.path.relpath(path, working_dir)).as_posix())}" for path in paths
        )
        template_flags = "".join(
            f" --patch-templates {shlex.quote(Path(os.path.relpath(builtin_templates / name, working_dir)).as_posix())}"
            for name in templates
        )
        bash(
            f"{tool} init{' --project ' + project_name if project_name is not None else ''}"
            f" --no-sample{provisioner_flags}{template_flags}",
            cwd=working_dir,
        )

        # Pre-seed each workload's instance suffix so generated names are deterministic
        if substrate == "k8s":
            state_path = output.parent / f".{tool}" / "state.yaml"
            state = ToolState.model_validate(yaml.safe_load(state_path.read_text(encoding="utf-8")) or {})
            for workload in workloads:
                workload_name = Path(workload.file_name).stem
                state.workloads.setdefault(workload_name, {})["instance_suffix"] = (
                    "-" + sha256(workload_name.encode()).hexdigest()[:10]
                )
            write_file(state_path, dump_yaml(state.model_dump()))

        # Expand each workload's ${NAME} holes into per-run copies
        copies_directory = Path(stack.enter_context(TemporaryDirectory()))
        # List the variable sources lowest-priority first so later ones overwrite earlier ones
        variable_values = {
            variable: value
            for source in (images, service_shas, rendering_vars)
            for variable, value in source.items()
            if value
        }

        for workload in workloads:
            expanded = dump_yaml(workload.raw)
            for variable, value in variable_values.items():
                expanded = expanded.replace(f"${{{variable}}}", value)

            # Refuse holes that no source fills
            leftover = PLACEHOLDER.search(expanded)
            if leftover is not None:
                raise ValueError(
                    f"{workload.file_name}: ${{{leftover.group(1)}}} is set in none of the rendering vars, "
                    "the service shas, or the image lock"
                )

            (copies_directory / workload.file_name).write_text(expanded, "utf-8")

        # Run generate over the expanded copies
        rendered = " ".join((copies_directory / workload.file_name).as_posix() for workload in workloads)
        bash(f"{tool} generate {rendered} --output {output.as_posix()}", cwd=working_dir)


def load_workloads(score_dir: Path, names: list[str], lens: type[WorkloadDocument]) -> list[LoadedWorkload]:
    loaded: list[LoadedWorkload] = []
    for name in names:
        raw = load_yaml_file(score_dir / name)
        loaded.append(LoadedWorkload(name, raw, lens.parse(raw, name)))
    return loaded


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
