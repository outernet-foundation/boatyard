from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from bashrun.bash import bash_output
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)

from boatyard.config import (
    DEV_TOGGLES_ANNOTATION,
    K8sConfig,
    Yaml,
    Config,
    chart_lock_key,
    is_list,
    is_mapping,
    load_workload,
)
from boatyard.config_lock import ConfigLock
from boatyard.lock import env_entries
from boatyard.score import (
    MARKERS,
    dump_compose,
    marker_entry,
    render_score,
    resolve_markers,
    split_documents,
    write_generated_file,
)

GPU_POD_TEMPLATE = "gpu-pod.tpl"
SCALE_TO_ZERO_TEMPLATE = "scale-to-zero.tpl"

STORAGE_CLASS_VAR = "SCORE_STORAGE_CLASS"
CLOUD_STORAGE_CLASS = "hcloud-volumes"
LOCAL_STORAGE_CLASS = "local-path"


class ManifestMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str = ""
    name: str = ""


class ManifestDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str = ""
    metadata: ManifestMetadata = Field(default_factory=ManifestMetadata)


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
    score_dir = root / config.score_dir
    rendering_vars = {**k8s.vars, STORAGE_CLASS_VAR: storage_class}

    compose_workloads = {name for bundle in config.compose_bundles() for name in bundle.workloads}
    for name in k8s.workloads:
        if name in compose_workloads:
            continue
        k8s_annotations = load_workload(score_dir, name).metadata.annotations
        if DEV_TOGGLES_ANNOTATION in k8s_annotations:
            raise ValueError(
                f"{name}: {DEV_TOGGLES_ANNOTATION} is compose dev-overlay vocabulary; this workload renders k8s-only"
            )

    render_score(
        config,
        is_k8s=True,
        workloads=k8s.workloads,
        rendering_vars=rendering_vars,
        templates=[GPU_POD_TEMPLATE, SCALE_TO_ZERO_TEMPLATE],
        output=output_path,
        images=images,
        service_shas=service_shas,
        tool_environment=tool_environment,
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
                    resolved = resolve_markers(document, root, images, path.as_posix())
                    target = values_dir / path.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    write_generated_file(target, dump_compose(resolved))
                    values_arguments.append(target.as_posix())
                values = "".join(f" --values {argument}" for argument in values_arguments)
                rendered = bash_output(f"helm template {chart_name} oci://{entry}{values}", env=tool_environment)
                chart_blocks.extend(split_documents(rendered))
        write_generated_file(
            output_path,
            output_path.read_text(encoding="utf-8") + "".join(f"---\n{block}" for block in chart_blocks),
        )

    # Sort documents by kind, namespace, and name.
    documents = split_documents(output_path.read_text(encoding="utf-8"))
    decorated: list[tuple[str, str, str, int, str]] = []
    for index, block in enumerate(documents):
        manifest = ManifestDocument.model_validate(yaml.safe_load(block) or {})
        decorated.append((manifest.kind, manifest.metadata.namespace, manifest.metadata.name, index, block))
    decorated.sort()
    write_generated_file(output_path, "".join(f"---\n{block}" for *_, block in decorated))

    shutil.rmtree(state_dir, ignore_errors=True)


def contains_marker(node: Yaml) -> bool:
    if marker_entry(node, MARKERS) is not None:
        return True

    if is_mapping(node):
        return any(contains_marker(value) for value in node.values())

    if is_list(node):
        return any(contains_marker(item) for item in node)

    return False
