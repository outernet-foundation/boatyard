import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from boatyard.cli import app

runner = CliRunner()

WITH_AXIS = "ARG PY_IMAGE\nARG TORCH_DEVICE\nFROM ${PY_IMAGE}\n"
DEPENDENT = "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n"
EXTERNAL = "ARG PY_IMAGE\nFROM ${PY_IMAGE}\n"

PY_BASE = {"PY_IMAGE": "docker.io/library/python:3.13-slim"}


def neural_base(axis: dict[str, list[str]] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"dockerfile": "workloads/neural-networks-base/Dockerfile"}
    if axis is not None:
        entry["axis"] = axis
    return {"NEURAL_NETWORKS_BASE": entry}


def _write_repo(
    root: Path,
    dockerfiles: dict[str, str],
    *,
    config: dict[str, Any] | None = None,
    axis: dict[str, list[str]] | None = None,
) -> None:
    score = root / "stack" / "score"
    score.mkdir(parents=True, exist_ok=True)
    referenced: list[str] = []
    for service, text in dockerfiles.items():
        directory = root / "workloads" / service
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Dockerfile").write_text(text, encoding="utf-8")
        (score / f"{service}.yaml").write_text(
            "metadata:\n"
            f"  name: {service}\n"
            "  annotations:\n"
            f"    outernet.internal/dockerfile: {service}=workloads/{service}/Dockerfile\n"
            "containers:\n"
            f"  {service}: {{}}\n",
            encoding="utf-8",
        )
        referenced.append(f"{service}.yaml")
    payload: dict[str, Any] = {"requires": ">=0", "project_name": "demo"}
    if dockerfiles:
        payload["repository"] = "ghcr.io/example"
        payload["platforms"] = ["linux/amd64"]
        payload.setdefault("base_images", PY_BASE)
        payload.setdefault("k8s", {"workloads": referenced})
        if axis is not None:
            payload["base_images"] = {**PY_BASE, **neural_base(axis)}
    if config is not None:
        payload.update(config)
    payload.setdefault("services", {"ext": {"ref": "oci://ghcr.io/org/ext:0.1.0"}})
    (root / "boatyard.yml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _invoke_matrix(
    monkeypatch: Any,
    tmp_path: Path,
    dockerfiles: dict[str, str],
    config: dict[str, Any] | None = None,
    axis: dict[str, list[str]] | None = None,
) -> Any:
    _write_repo(tmp_path, dockerfiles, config=config, axis=axis)
    monkeypatch.chdir(tmp_path)
    return runner.invoke(app, ["docker-build-matrix"])


def test_bases_group_with_their_dependents_per_variant(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(
        monkeypatch,
        tmp_path,
        {
            "api": EXTERNAL,
            "postgres": EXTERNAL,
            "neural-networks-base": WITH_AXIS,
            "reconstructor": DEPENDENT,
            "localizer": DEPENDENT,
        },
        axis={"TORCH_DEVICE": ["cuda", "rocm"]},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"targets": "api postgres", "variant": "common"},
        {"targets": "localizer/cuda neural-networks-base/cuda reconstructor/cuda", "variant": "cuda"},
        {"targets": "localizer/rocm neural-networks-base/rocm reconstructor/rocm", "variant": "rocm"},
    ]


def test_a_service_arg_without_a_declared_axis_stays_common(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(monkeypatch, tmp_path, {"api": WITH_AXIS, "postgres": EXTERNAL})

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "api postgres", "variant": "common"}]


def test_declared_values_reach_the_legs_verbatim(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(
        monkeypatch,
        tmp_path,
        {"neural-networks-base": WITH_AXIS, "reconstructor": DEPENDENT},
        axis={"TORCH_DEVICE": ["rocm", "cuda"]},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"targets": "neural-networks-base/cuda reconstructor/cuda", "variant": "cuda"},
        {"targets": "neural-networks-base/rocm reconstructor/rocm", "variant": "rocm"},
    ]


def test_dependents_with_no_declared_axis_group_with_their_base(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(
        monkeypatch,
        tmp_path,
        {
            "base": EXTERNAL,
            "app": "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n",
        },
        config={"base_images": {**PY_BASE, "BASE_IMAGE": {"dockerfile": "workloads/base/Dockerfile"}}},
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "app base", "variant": "common"}]


def test_independent_services_share_the_common_leg(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(monkeypatch, tmp_path, {"api": EXTERNAL, "postgres": EXTERNAL})

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "api postgres", "variant": "common"}]


def test_single_service_repo_emits_matrix_of_one(monkeypatch: Any, tmp_path: Path) -> None:
    result = _invoke_matrix(monkeypatch, tmp_path, {"livekit-token": EXTERNAL})

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "livekit-token", "variant": "common"}]


def test_repo_uniform_platforms_leave_the_legs_unchanged(monkeypatch: Any, tmp_path: Path) -> None:
    _write_repo(
        tmp_path,
        {"api": EXTERNAL, "zed-capture": EXTERNAL},
        config={"platforms": ["linux/arm64"]},
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["docker-build-matrix"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [{"targets": "api zed-capture", "variant": "common"}]


def test_repo_without_config_emits_empty(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["docker-build-matrix"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_config_without_services_emits_empty(monkeypatch: Any, tmp_path: Path) -> None:
    _write_repo(tmp_path, {}, config={"requires": ">=0.1"})
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["docker-build-matrix"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
