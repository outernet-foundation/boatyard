from __future__ import annotations

import os
import re
import shlex
from contextlib import ExitStack
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

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
    PUBLISH_PORT_LABEL,
    ROUTE_PATH_LABEL,
    ROUTE_PORT_LABEL,
    ROUTE_STRIP_LABEL,
    LoadedWorkload,
    RouteClaim,
    listener_port_reference,
    require_port_range,
    select_provisioner_files,
)
from boatyard.yaml import Yaml, is_mapping, load_yaml_model
from boatyard.markers import MarkerContext, compose_image_reference, resolve_markers

PATCH_TEMPLATES_DIR = "score_patch_templates"

SECRETS_OUTPUT = Path("stack/generated/secrets.txt")

STRING_FORM_FIELDS = ("state", "outputs", "volumes", "services", "manifests")

PROVISIONER_ROUTE_KEY = "route"

STATE_TEMPLATE_PATTERN = re.compile(r"\{\{\s*\.State\.([a-zA-Z0-9_.-]+)\s*\}\}")


class ProvisionerRouteClaim(RouteClaim):
    # The stanza-surface spelling: a literal port or a whole-value {{ .State.<name> }} template
    port: str | int

    @field_validator("port", mode="before")
    @classmethod
    def _refuse_boolean_port(cls, value: Yaml) -> Yaml:
        # Pydantic coerces True to the int 1 before the literal-or-template resolution runs
        match value:
            case bool():
                raise ValueError("route.port must be an integer, not a boolean")
            case _:
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
    listener_exports: dict[str, str],
    variable_values: dict[str, str],
    images: dict[str, str],
    templates: list[str],
    output: Path,
) -> None:
    # Derive the per-substrate names and create the output parent
    root = config.root
    score_dir = root / config.score_dir
    tool = f"score-{substrate}"
    # Run both tools in the output directory because both write their state dir into the cwd
    working_dir = output.parent
    project_name = None if substrate == "k8s" else config.project_name
    output.parent.mkdir(parents=True, exist_ok=True)

    # The lock images join as bindings underneath the caller's values, and each export
    # family refuses any key the authored surface already spells
    for family, exports in (("route path", route_exports), ("listener port", listener_exports)):
        collisions = sorted(set(exports) & ({*images, *variable_values}))
        if collisions:
            raise ValueError(f"the {family} export {collisions[0]} collides with an authored rendering value")
    crossed = sorted(set(route_exports) & set(listener_exports))
    if crossed:
        raise ValueError(f"the route path export {crossed[0]} collides with the listener port export {crossed[0]}")
    variable_values = {**images, **variable_values, **route_exports, **listener_exports}

    with (
        as_file(files("boatyard") / PATCH_TEMPLATES_DIR) as builtin_templates,
        ExitStack() as stack,
    ):
        # Select the rendering's provisioner files that declare a referenced type
        selected = select_provisioner_files(score_dir, substrate, workloads)

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
                provisioner = AuthoredProvisioner.model_validate(resolved)
                compiled_entries.append({
                    key: dump_yaml(value) if key in STRING_FORM_FIELDS and value is not None else value
                    for key, value in provisioner.model_dump(exclude_unset=True).items()
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
        expanded_paths: list[str] = []

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

            copy_path = copies_directory / workload.file_name
            copy_path.write_text(expanded, "utf-8")
            expanded_paths.append(copy_path.as_posix())

        # Run generate over the expanded copies
        rendered = " ".join(expanded_paths)
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
        name = Path(workload.file_name).stem.upper().replace("-", "_") + "_ROUTE_PATH"
        if name in exports:
            raise ValueError(
                f"the workloads {owners[name]} and {workload.file_name} spell one route path export {name}"
            )
        exports[name] = resolved.path.removesuffix("/*")
        owners[name] = workload.file_name
    return exports


def listener_port_exports(workloads: list[LoadedWorkload]) -> dict[str, str]:
    # One <WORKLOAD>_LISTENER_<NAME> export per non-internal listener, riding the same tool
    # env the route path exports ride; internal listeners never ride the service surface
    exports: dict[str, str] = {}
    owners: dict[str, str] = {}
    for workload in workloads:
        prefix = Path(workload.file_name).stem.upper().replace("-", "_") + "_LISTENER_"
        for listener_name, port in workload.document.routable_listener_ports.items():
            name = prefix + listener_name.upper().replace("-", "_")
            if name in exports:
                raise ValueError(
                    f"the workloads {owners[name]} and {workload.file_name} spell one listener port export {name}"
                )
            exports[name] = str(port)
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
    state = entry.get("state")
    services = {name: claimed_service_stanza(name, stanza, source, state) for name, stanza in services_value.items()}
    return {**entry, "services": services}


def claimed_service_stanza(name: str, stanza: Yaml, source: str, state: Yaml) -> Yaml:
    if not is_mapping(stanza):
        return stanza
    site = f"{source}: services.{name}"
    if PROVISIONER_ROUTE_KEY not in stanza:
        # score-compose silently drops unknown service keys, so a port spelling without its
        # route claim would vanish; the pairing is refused here instead
        if "port" in stanza:
            raise ValueError(f"{site}: port: rides a route: claim")
        return stanza
    claim_value = stanza[PROVISIONER_ROUTE_KEY]
    if not is_mapping(claim_value):
        raise ValueError(f"{site}: route: takes the claim as a mapping (path, port, strip)")
    claim = ProvisionerRouteClaim.model_validate(dict(claim_value))
    port = provisioner_route_port(claim.port, state, site)
    annotations = stanza.get("annotations")
    if annotations is not None and not is_mapping(annotations):
        raise ValueError(f"{site}: annotations: must be a mapping")
    spelled = dict(annotations) if annotations is not None else {}
    for label in (ROUTE_PATH_LABEL, ROUTE_PORT_LABEL, ROUTE_STRIP_LABEL):
        if label in spelled:
            raise ValueError(f"{site}: annotates {label} directly; declare route: instead")
    spelled[ROUTE_PATH_LABEL] = claim.path
    spelled[ROUTE_PORT_LABEL] = str(port)
    if claim.strip:
        spelled[ROUTE_STRIP_LABEL] = "true"
    return {
        **{key: value for key, value in stanza.items() if key != PROVISIONER_ROUTE_KEY},
        "annotations": spelled,
    }


def provisioner_route_port(value: str | int, state: Yaml, site: str) -> int:
    # A literal port rides through; a string is one {{ .State.<name> }} template rendered
    # against the provisioner's own state (the state the templating engine resolves the
    # stanza's other templates against), so the port label carries the integer
    shapes = f"{site}: route.port: must be a literal port in the range 1-65535 or a {{{{ .State.<name> }}}} template"
    match value:
        case int():
            return require_port_range(value, f"{site}: route.port")
        case str():
            pass
    template = STATE_TEMPLATE_PATTERN.fullmatch(value)
    if template is None:
        raise ValueError(shapes)
    resolved: Yaml = state
    for segment in template.group(1).split("."):
        if not is_mapping(resolved) or segment not in resolved:
            raise ValueError(f"{site}: route.port: {value!r} names no entry the state declares")
        resolved = resolved[segment]
    rendered = str(resolved)
    if not rendered.isdigit() or not 1 <= int(rendered) <= 65535:
        raise ValueError(f"{site}: route.port: {value!r} renders to {rendered!r}, not a port in the range 1-65535")
    return int(rendered)


def injected_workload(config: ConfigFile, workload: LoadedWorkload, variable_values: dict[str, str]) -> Yaml:
    # The authored blocks are boatyard's grammar; the seam composes the tools' standalone document
    # from the lens instead: service.ports derives from the non-internal listeners, the resolved
    # claim and publish set ride metadata.annotations as the interchange labels the tools copy
    # onto the rendered stanza, the image reference composes from the container's declared
    # source, and probe and variable listener names rewrite to their ports.
    document = workload.document
    tool = document.model_dump(by_alias=True, exclude_unset=True, exclude={"boatyard", "listeners"})
    routable = document.routable_listener_ports
    if routable:
        tool["service"] = {"ports": {name: {"port": port, "targetPort": port} for name, port in routable.items()}}

    block = document.boatyard
    labels: dict[str, str] = {}
    resolved = document.resolved_route
    if resolved is not None:
        labels[ROUTE_PATH_LABEL] = resolved.path
        labels[ROUTE_PORT_LABEL] = str(resolved.port)
        if resolved.strip_prefix:
            labels[ROUTE_STRIP_LABEL] = "true"
    if block is not None and block.publish:
        labels[PUBLISH_PORT_LABEL] = ",".join(
            f"{entry.published}:{entry.target}/{entry.protocol}" for entry in block.publish
        )
    if labels:
        metadata = tool.setdefault("metadata", {})
        annotations = metadata.setdefault("annotations", {})
        annotations.update(labels)

    if document.containers:
        containers = tool["containers"]
        sources = document.container_sources()
        for container, (kind, value) in sources.items():
            if kind == "lock":
                reference = variable_values[value]
            else:
                stem = dockerfile_stem(value)
                image = config.images[stem]
                if config.repository is None:
                    raise ValueError(
                        f"{workload.file_name}: containers.{container}: repository is required by the build set"
                    )
                variant = variable_values.get(image.axis) if image.axis is not None else None
                if image.axis is not None and variant is None:
                    raise ValueError(
                        f"{workload.file_name}: containers.{container}: {image.name} carries the axis "
                        f"{image.axis}, which this rendering selects no value for"
                    )
                tag = variable_values[image.sha_env]
                reference = compose_image_reference(config.repository, image, tag, variant)
            containers[container]["image"] = reference
        rewrite_listener_references(containers, workload)
    return tool


def rewrite_listener_references(containers: dict[str, Any], workload: LoadedWorkload) -> None:
    # The lens already refused every probe port and ${listeners.<name>.port} reference that
    # names no declared listener, so the rewrites are plain substitutions over the dumped
    # document keyed by the validated container shapes; the variable port lands as a string
    # because the score schemas type variables as strings and the old self-port resource
    # idiom rendered the string form
    listeners = workload.document.listener_ports
    for name, container in workload.document.containers.items():
        entry = containers[name]
        for probe, block in (
            ("readinessProbe", container.readiness_probe),
            ("livenessProbe", container.liveness_probe),
        ):
            if block is None or block.http_get is None:
                continue
            entry[probe]["httpGet"]["port"] = listeners[block.http_get.port]
        variables = container.variables
        if variables is None:
            continue
        for key, value in variables.items():
            if not isinstance(value, str):
                continue
            listener = listener_port_reference(value)
            if listener is not None:
                entry["variables"][key] = str(listeners[listener])


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
