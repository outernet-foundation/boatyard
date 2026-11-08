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

from boatyard.models.config_file import (
    K8sConfig,
    PLACEHOLDER,
    ConfigFile,
    chart_lock_key,
)
from boatyard.models.k8s_pins import load_pins
from boatyard.models.lock_file import LockFile
from boatyard.models.workload import WorkloadDocument, load_workloads
from boatyard.markers import MarkerContext, resolve_markers
from boatyard.paths import K8S_PINS_PATH
from boatyard.yaml import Yaml, is_list, is_mapping
from boatyard.score import (
    dump_yaml,
    render_score,
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
    config: ConfigFile,
    k8s: K8sConfig,
    lock: LockFile,
    local: bool,
) -> tuple[Path, set[str]]:
    score_dir = config.root / config.score_dir
    index_images = lock.index_references()
    workloads = load_workloads(score_dir, k8s.workloads, WorkloadDocument)

    output_path = config.root / (config.k8s_local_output if local else config.k8s_output)

    render_score(
        config,
        substrate="k8s",
        workloads=workloads,
        variable_values={
            STORAGE_CLASS_VAR: LOCAL_STORAGE_CLASS if local else CLOUD_STORAGE_CLASS,
            **k8s.vars,
            **(
                {image.sha_env: image.compute_sha(config.root) for image in config.images.values()}
                if local
                else load_pins(
                    config.root / K8S_PINS_PATH,
                    {
                        name
                        for workload in workloads
                        for name in PLACEHOLDER.findall(dump_yaml(workload.raw))
                        if name.endswith("_SHA")
                    },
                )
            ),
        },
        templates=[GPU_POD_TEMPLATE, SCALE_TO_ZERO_TEMPLATE],
        output=output_path,
        images=index_images,
    )

    # Render the charts through helm template.
    chart_blocks: list[str] = []
    with TemporaryDirectory(prefix="resolved-values-") as values_directory:
        values_dir = Path(values_directory)
        for chart_name in k8s.charts:
            # Every values file rides through marker resolution, so the literal-image guard is universal.
            path = config.root / config.score_dir / "k8s" / "values" / f"{chart_name}.yaml"
            values_path = values_dir / path.relative_to(config.root)
            values_path.parent.mkdir(parents=True, exist_ok=True)
            write_file(
                values_path,
                dump_yaml(
                    resolve_markers(
                        yaml.safe_load(path.read_text(encoding="utf-8")),
                        MarkerContext(config.root, index_images, path.as_posix()),
                    )
                ),
            )

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
    decorated: list[tuple[str, str, str, int, str]] = []
    for index, block in enumerate(split_documents(output_path.read_text(encoding="utf-8"))):
        manifest = ManifestDocument.model_validate(yaml.safe_load(block) or {})
        decorated.append((manifest.kind, manifest.metadata.namespace, manifest.metadata.name, index, block))
    decorated.sort()
    write_file(output_path, "".join(f"---\n{block}" for *_, block in decorated))

    shutil.rmtree(output_path.parent / ".score-k8s", ignore_errors=True)

    # The mint set is the rendering's consumed secret refs, walked from the output.
    secret_name = f"{config.project_name}-secrets"
    consumed = {
        key
        for block in split_documents(output_path.read_text(encoding="utf-8"))
        for key in collect_secret_keys(yaml.safe_load(block), secret_name)
    }

    return output_path, consumed


def collect_secret_keys(node: Yaml, secret_name: str) -> set[str]:
    keys: set[str] = set()
    if is_mapping(node):
        reference = node.get("secretKeyRef")
        if is_mapping(reference) and reference.get("name") == secret_name:
            key = reference.get("key")
            if isinstance(key, str):
                keys.add(key)

        for value in node.values():
            keys |= collect_secret_keys(value, secret_name)

    elif is_list(node):
        for item in node:
            keys |= collect_secret_keys(item, secret_name)
    return keys
