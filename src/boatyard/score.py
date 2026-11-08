from __future__ import annotations

import os
import re
import shlex
from contextlib import ExitStack
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from bashrun.bash import bash
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)

from boatyard.config.config import (
    PLACEHOLDER,
    Substrate,
    ConfigFile,
)
from boatyard.config.workload import LoadedWorkload, select_provisioner_files
from boatyard.yaml import Yaml, is_mapping, load_yaml_model
from boatyard.markers import MarkerContext, resolve_markers

PATCH_TEMPLATES_DIR = "score_patch_templates"

SECRETS_OUTPUT = Path("stack/generated/secrets.txt")

STRING_FORM_FIELDS = ("state", "outputs", "volumes", "services", "manifests")


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


class ToolState(BaseModel):
    # Type only the workloads shape of the tool-owned state file
    model_config = ConfigDict(extra="allow")

    workloads: dict[str, dict[str, Yaml]] = Field(default_factory=dict)


def _represent_multiline(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    return yaml.ScalarNode("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


LiteralBlockDumper.add_representer(str, _represent_multiline)


def render_score(
    config: ConfigFile,
    substrate: Substrate,
    workloads: list[LoadedWorkload],
    variable_values: dict[str, str],
    images: dict[str, str],
    templates: list[str],
    output: Path,
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

    # The lock images ride as bindings underneath the caller's values
    variable_values = {**images, **variable_values}

    with (
        as_file(files("boatyard") / PATCH_TEMPLATES_DIR) as builtin_templates,
        ExitStack() as stack,
    ):
        # Select the rendering's provisioner files that declare a referenced type
        selected = select_provisioner_files(score_dir, rendering, workloads)

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
            env=variable_values,
        )

        # Pre-seed each workload's instance suffix so generated names are deterministic
        if substrate == "k8s":
            state_path = output.parent / f".{tool}" / "state.yaml"
            state = load_yaml_model(ToolState, state_path)
            for workload in workloads:
                workload_name = Path(workload.file_name).stem
                state.workloads.setdefault(workload_name, {})["instance_suffix"] = (
                    "-" + sha256(workload_name.encode()).hexdigest()[:10]
                )
            write_file(state_path, dump_yaml(state.model_dump()))

        # Expand each workload's ${NAME} slots into per-run copies
        copies_directory = Path(stack.enter_context(TemporaryDirectory()))

        for workload in workloads:
            expanded = dump_yaml(workload.raw)
            for variable, value in variable_values.items():
                expanded = expanded.replace(f"${{{variable}}}", value)

            # Refuse slots that no source fills
            leftover = PLACEHOLDER.search(expanded)
            if leftover is not None:
                raise ValueError(
                    f"{workload.file_name}: ${{{leftover.group(1)}}} is set in none of the rendering vars, "
                    "the service shas, or the image lock"
                )

            (copies_directory / workload.file_name).write_text(expanded, "utf-8")

        # Run generate over the expanded copies
        rendered = " ".join((copies_directory / workload.file_name).as_posix() for workload in workloads)
        bash(f"{tool} generate {rendered} --output {output.as_posix()}", cwd=working_dir, env=variable_values)


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
