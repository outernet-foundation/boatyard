from __future__ import annotations

import copy
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

from boatyard.models.config_file import (
    PLACEHOLDER,
    Substrate,
    ConfigFile,
)
from boatyard.models.image import dockerfile_stem
from boatyard.models.workload import (
    ROUTE_ANNOTATION,
    ROUTE_PORT_ANNOTATION,
    LoadedWorkload,
    parse_route_annotation,
    route_path_export_name,
    select_provisioner_files,
)
from boatyard.yaml import Yaml, is_mapping, load_yaml_model
from boatyard.markers import MarkerContext, compose_image_reference, resolve_markers

PATCH_TEMPLATES_DIR = "score_patch_templates"

SECRETS_OUTPUT = Path("stack/generated/secrets.txt")

STRING_FORM_FIELDS = ("state", "outputs", "volumes", "services", "manifests")

PROVISIONER_ROUTE_KEY = "route"
PROVISIONER_PORT_KEY = "port"


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
    route_exports: dict[str, str],
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

    # The lock images join as bindings underneath the caller's values
    collisions = sorted(set(route_exports) & ({*images, *variable_values}))
    if collisions:
        raise ValueError(f"the route path export {collisions[0]} collides with an authored rendering value")
    variable_values = {**images, **variable_values, **route_exports}

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
            context = MarkerContext(
                root=root,
                images=images,
                source=name,
                repository=config.repository,
                builds=config.images,
            )
            compiled_entries: list[Yaml] = []
            for entry in entries:
                resolved = resolve_markers(entry, context)
                resolved = translate_service_claims(resolved, name)
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
            expanded = dump_yaml(injected_workload(config, workload, variable_values))
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


def route_path_exports(workloads: list[LoadedWorkload]) -> dict[str, str]:
    # One <WORKLOAD>_ROUTE_PATH export per claiming workload, riding the same tool env the
    # sha and lock exports ride; the value is the claim's concrete path (the /* tail drops)
    exports: dict[str, str] = {}
    owners: dict[str, str] = {}
    for workload in workloads:
        resolved = workload.document.resolved_route
        if resolved is None:
            continue
        name = route_path_export_name(Path(workload.file_name).stem)
        if name in exports:
            raise ValueError(
                f"the workloads {owners[name]} and {workload.file_name} spell one route path export {name}"
            )
        exports[name] = resolved.path.removesuffix("/*")
        owners[name] = workload.file_name
    return exports


def translate_service_claims(entry: Yaml, source: str) -> Yaml:
    # A provisioner service stanza authors route:/port: keys; score-compose silently drops
    # unknown service keys, so the compile pass pops them and spells the same claim and port
    # annotations the workload surface renders
    if not is_mapping(entry):
        return entry
    services_value = entry.get("services")
    if not is_mapping(services_value):
        return entry
    services = {name: claimed_service_stanza(name, stanza, source) for name, stanza in services_value.items()}
    return {**entry, "services": services}


def claimed_service_stanza(name: str, stanza: Yaml, source: str) -> Yaml:
    if not is_mapping(stanza) or (PROVISIONER_ROUTE_KEY not in stanza and PROVISIONER_PORT_KEY not in stanza):
        return stanza
    route_value = stanza.get(PROVISIONER_ROUTE_KEY)
    port_value = stanza.get(PROVISIONER_PORT_KEY)
    site = f"{source}: services.{name}"
    if route_value is None:
        raise ValueError(f"{site}: {PROVISIONER_PORT_KEY}: rides a {PROVISIONER_ROUTE_KEY}: claim")
    match route_value:
        case str():
            pass
        case _:
            raise ValueError(f"{site}: {PROVISIONER_ROUTE_KEY}: takes the claim grammar as a string")
    claim = parse_route_annotation(route_value)
    if claim.port_name is not None:
        raise ValueError(
            f"{site}: {PROVISIONER_ROUTE_KEY}: takes no @port qualifier; {PROVISIONER_PORT_KEY}: names the listen port"
        )
    if port_value is None:
        raise ValueError(
            f"{site}: {PROVISIONER_ROUTE_KEY}: requires the {PROVISIONER_PORT_KEY}: key naming the listen port"
        )
    if isinstance(port_value, bool) or not isinstance(port_value, int) or not 1 <= port_value <= 65535:
        raise ValueError(f"{site}: {PROVISIONER_PORT_KEY}: must be a literal port in the range 1-65535")
    annotations = stanza.get("annotations")
    if annotations is not None and not is_mapping(annotations):
        raise ValueError(f"{site}: annotations: must be a mapping")
    spelled = dict(annotations) if annotations is not None else {}
    for annotation in (ROUTE_ANNOTATION, ROUTE_PORT_ANNOTATION):
        if annotation in spelled:
            raise ValueError(f"{site}: annotates {annotation} directly; declare {PROVISIONER_ROUTE_KEY}: instead")
    spelled[ROUTE_ANNOTATION] = route_value
    spelled[ROUTE_PORT_ANNOTATION] = str(port_value)
    return {
        **{key: value for key, value in stanza.items() if key not in (PROVISIONER_ROUTE_KEY, PROVISIONER_PORT_KEY)},
        "annotations": spelled,
    }


def injected_workload(config: ConfigFile, workload: LoadedWorkload, variable_values: dict[str, str]) -> Yaml:
    # The image reference is composed from the container's declared source and injected
    # before score sees the document; it is never authored in the workload file.
    sources = workload.document.container_sources()
    if not sources:
        return workload.raw
    document = copy.deepcopy(workload.raw)
    containers = document["containers"]
    if not is_mapping(containers):
        raise ValueError(f"{workload.file_name}: containers must be a mapping")
    for container, (kind, value) in sources.items():
        if kind == "lock":
            reference = variable_values.get(value)
            if reference is None:
                raise ValueError(f"{workload.file_name}: containers.{container}: {value} names no lock entry")
        else:
            stem = dockerfile_stem(value)
            image = config.images.get(stem)
            if image is None:
                raise ValueError(
                    f"{workload.file_name}: containers.{container}: the dockerfile {value!r} names no image in the build set"
                )
            if config.repository is None:
                raise ValueError(
                    f"{workload.file_name}: containers.{container}: repository is required by the build set"
                )
            variant = None
            if image.axis is not None:
                variant = variable_values.get(image.axis)
                if variant is None:
                    raise ValueError(
                        f"{workload.file_name}: containers.{container}: {image.name} carries the axis "
                        f"{image.axis}, which this rendering selects no value for"
                    )
            tag = variable_values.get(image.sha_env)
            if tag is None:
                raise ValueError(
                    f"{workload.file_name}: containers.{container}: {image.sha_env} names no tree tag in this rendering"
                )
            reference = compose_image_reference(config.repository, image, tag, variant)
        entry = containers[container]
        if not is_mapping(entry):
            raise ValueError(f"{workload.file_name}: containers.{container} must be a mapping")
        entry["image"] = reference
    return document


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
