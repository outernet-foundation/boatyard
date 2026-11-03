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
    WorkloadDocument,
    Yaml,
    Config,
    chart_lock_key,
    is_list,
    is_mapping,
)
from boatyard.config_lock import ConfigLock
from boatyard.lock import env_entries
from boatyard.score import (
    MARKERS,
    SECRETS_FILE,
    SecretsDeclaration,
    collect_secret_keys,
    dump_yaml,
    marker_entry,
    render_score,
    resolve_markers,
    split_documents,
    write_file,
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
    config: Config, lock: ConfigLock, service_shas: dict[str, str], local: bool, secrets: SecretsDeclaration
) -> Path:
    k8s = config.k8s
    if k8s is None:
        raise ValueError("the k8s target requires a k8s section")
    root = config.root
    storage_class = LOCAL_STORAGE_CLASS if local else CLOUD_STORAGE_CLASS
    output_path = root / (config.k8s_local_output if local else config.k8s_output)
    # Run score-k8s in the output's directory, where it writes its .score-k8s state.
    images = env_entries(lock)
    state_dir = output_path.parent / ".score-k8s"
    score_dir = root / config.score_dir
    rendering_vars = {**k8s.vars, STORAGE_CLASS_VAR: storage_class}

    compose_workloads = {workload for _, entry in config.local_service_entries() for workload in entry.workloads}
    for name in k8s.workloads:
        if name in compose_workloads:
            continue
        k8s_annotations = WorkloadDocument.load(score_dir / name).metadata.annotations
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
    )

    # Render the charts through helm template.
    if k8s.charts:
        chart_blocks: list[str] = []
        with TemporaryDirectory(prefix="resolved-values-") as values_directory:
            values_dir = Path(values_directory)
            for chart_name in k8s.charts:
                # Pass a marker-free values file through untouched; resolve markers into a tempdir copy.
                path = root / config.score_dir / "k8s" / "values" / f"{chart_name}.yaml"
                document = yaml.safe_load(path.read_text(encoding="utf-8"))

                if not contains_marker(document):
                    values_path = path
                else:
                    resolved = resolve_markers(document, root, images, path.as_posix())
                    values_path = values_dir / path.relative_to(root)
                    values_path.parent.mkdir(parents=True, exist_ok=True)
                    write_file(values_path, dump_yaml(resolved))

                # Render the chart through helm template with the values file and the locked chart reference.
                chart_blocks.extend(
                    split_documents(
                        bash_output(
                            f"helm template {chart_name} oci://"
                            f"{lock.require(chart_lock_key(chart_name), f'chart {chart_name!r}')}"
                            f" --values {values_path.as_posix()}"
                        )
                    )
                )

        # Append the rendered charts to the k8s output, preserving any existing content.
        write_file(
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
    write_file(output_path, "".join(f"---\n{block}" for *_, block in decorated))

    shutil.rmtree(state_dir, ignore_errors=True)

    # Validate the consumed secrets against the declaration.
    consumed: set[str] = set()
    for block in split_documents(output_path.read_text(encoding="utf-8")):
        collect_secret_keys(yaml.safe_load(block), f"{config.project_name}-secrets", consumed)
    secrets.validate_consumed(consumed, "k8s", SECRETS_FILE.as_posix())

    return output_path


def contains_marker(node: Yaml) -> bool:
    if marker_entry(node, MARKERS) is not None:
        return True

    if is_mapping(node):
        return any(contains_marker(value) for value in node.values())

    if is_list(node):
        return any(contains_marker(item) for item in node)

    return False
