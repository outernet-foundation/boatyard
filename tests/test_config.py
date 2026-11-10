import importlib.metadata
import re
from typing import Any
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from boatyard.models.config_file import (
    DEV_SENTINEL,
    ConfigFile,
    ServiceEntry,
    ServiceGroup,
    bundle_lock_key,
)
from boatyard.models.lock_file import LOCK_PATH, LockFile
from boatyard.models.workload import WorkloadDocument
from boatyard.yaml import Yaml, load_yaml_model

EDGE_PLATFORMS = ["linux/amd64"]

DIGEST = "sha256:" + "a" * 64
SHA_TAG = "a" * 40


def bundle_entries(*qualified_names: str) -> dict[str, dict[str, object]]:
    return {
        bundle_lock_key(name): {
            "reference": f"ghcr.io/org/repo/{name}:1.0.0@" + DIGEST,
            "services": [],
            "routes": [],
            "ports": [],
            "container_names": [],
        }
        for name in qualified_names
    }


def write_config(tmp_path: Path, payload: dict[str, object]) -> None:
    payload.setdefault("project_name", "example")
    payload.setdefault("services", {"ext": {"ref": "oci://ghcr.io/org/ext:0.1.0"}})
    (tmp_path / "boatyard.yml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def write_lock(
    tmp_path: Path,
    entries: dict[str, str] | None = None,
    bundles: dict[str, dict[str, object]] | None = None,
) -> None:
    (tmp_path / LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    images = (
        entries
        if entries is not None
        else {"ALPINE_IMAGE": "ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.21.0@" + DIGEST}
    )
    document: dict[str, object] = {"images": {name: {"index": reference} for name, reference in images.items()}}
    if bundles is not None:
        document["bundles"] = bundles
    (tmp_path / LOCK_PATH).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def write_edge_lock(tmp_path: Path, bundles: dict[str, dict[str, object]] | None = None) -> None:
    reference = "ghcr.io/outernet-foundation/mirror/docker.io/library/caddy:2.8.4-alpine@" + DIGEST
    write_lock(tmp_path, {"CADDY_IMAGE": reference}, bundles=bundles)


def write_dockerfile(tmp_path: Path, service: str, text: str, top: str = "workloads") -> None:
    directory = tmp_path / top / service
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Dockerfile").write_text(text, encoding="utf-8")


def write_workload(tmp_path: Path, file_name: str) -> None:
    score = tmp_path / "stack" / "score"
    score.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "metadata": {"name": Path(file_name).stem},
        "containers": {},
    }
    (score / file_name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def write_dockerfile_workload(tmp_path: Path, file_name: str) -> None:
    # A workload whose single container declares the dockerfile of its own stem
    stem = Path(file_name).stem
    write_dockerfile(tmp_path, stem, "ARG TOOLS_IMAGE\nFROM ${TOOLS_IMAGE}\n")
    score = tmp_path / "stack" / "score"
    score.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "metadata": {
            "name": stem,
            "annotations": {"outernet.internal/dockerfile": f"{stem}=workloads/{stem}/Dockerfile"},
        },
        "containers": {stem: {}},
    }
    (score / file_name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_load_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ConfigFile.load(tmp_path / "boatyard.yml")


def _installed_version(version: str) -> Any:
    def _version(_name: str) -> str:
        return version

    return _version


class TestRequires:
    def test_omitted_is_required(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"project_name": "example"})

        with pytest.raises(ValidationError, match="requires"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_empty_string_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ""})

        with pytest.raises(ValidationError, match="requires"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_invalid_specifier_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": "not-a-specifier"})

        with pytest.raises(ValidationError, match="requires"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_dev_sentinel_skips_check(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version(DEV_SENTINEL))
        write_config(tmp_path, {"requires": ">=0.1"})

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.requires == ">=0.1"

    def test_in_range_loads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version("0.1.18"))
        write_config(tmp_path, {"requires": ">=0.1.18"})

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.requires == ">=0.1.18"

    def test_dev_channel_prerelease_satisfies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version("0.1.19.dev5"))
        write_config(tmp_path, {"requires": ">=0.1.18"})

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None

    def test_out_of_range_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version("0.1.17"))
        write_config(tmp_path, {"requires": ">=0.1.18"})

        with pytest.raises(ValueError, match="does not satisfy requires"):
            ConfigFile.load(tmp_path / "boatyard.yml")


def test_load_rejects_duplicate_keys(tmp_path: Path) -> None:
    (tmp_path / "boatyard.yml").write_text('requires: ">=0.1"\nrequires: ">=0.2"\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"boatyard\.yml: line 2: duplicate key 'requires'"):
        ConfigFile.load(tmp_path / "boatyard.yml")


def test_load_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    write_config(tmp_path, {"requires": ">=0.1", "lifecycle_prefix": "ghcr.io/x/mirror"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ConfigFile.load(tmp_path / "boatyard.yml")


class TestOldSectionsDied:
    def test_generate_score_section_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "generate-score": {"project_name": "example"}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_publish_compose_section_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "publish-compose": {"repository": "ghcr.io/org/repo"}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_lifecycle_section_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "lifecycle": {"files": ["compose.yml"]}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_bundles_key_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "bundles": {"core": {"workloads": ["api.yaml"]}}},
        )

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestWorkloadReferences:
    def test_nested_service_workload_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "project_name": "example", "services": {"core": {"workloads": ["nested/api.yaml"]}}},
        )

        with pytest.raises(ValidationError, match=r"flat \.yaml file name"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_non_yaml_service_workload_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "project_name": "example", "services": {"core": {"workloads": ["api.yml"]}}},
        )

        with pytest.raises(ValidationError, match=r"flat \.yaml file name"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_nested_k8s_workload_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "k8s": {"workloads": ["nested/api.yaml"]}})

        with pytest.raises(ValidationError, match=r"flat \.yaml file name"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestVariants:
    def _write_gpu_tree(self, tmp_path: Path, *, with_axis: bool = True) -> None:
        # gpu FROMs the local neural-networks-base (which declares the TORCH_DEVICE axis)
        # when with_axis is on, and a remote base directly when off
        write_dockerfile(
            tmp_path,
            "gpu",
            "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n"
            if with_axis
            else "ARG TOOLS_IMAGE\nFROM ${TOOLS_IMAGE}\n",
        )
        if with_axis:
            write_dockerfile(
                tmp_path, "neural-networks-base", "ARG TOOLS_IMAGE\nARG TORCH_DEVICE\nFROM ${TOOLS_IMAGE}\n"
            )
        score = tmp_path / "stack" / "score"
        score.mkdir(parents=True, exist_ok=True)
        (score / "gpu.yaml").write_text(
            "metadata:\n"
            "  name: gpu\n"
            "  annotations:\n"
            "    outernet.internal/dockerfile: gpu=workloads/gpu/Dockerfile\n"
            "containers:\n"
            "  gpu: {}\n",
            encoding="utf-8",
        )

    def _write_group_config(
        self,
        tmp_path: Path,
        services: dict[str, Any],
        *,
        built_base: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._write_gpu_tree(tmp_path)
        payload: dict[str, Any] = {
            "requires": ">=0.1",
            "repository": "ghcr.io/example/repo",
            "platforms": ["linux/amd64"],
            "base_images": {
                "TOOLS_IMAGE": "docker.io/library/tools:1",
                "NEURAL_NETWORKS_BASE": built_base
                or {
                    "dockerfile": "workloads/neural-networks-base/Dockerfile",
                    "axis": {"TORCH_DEVICE": ["cuda", "rocm"]},
                },
            },
            "services": services,
        }
        payload.update(extra or {})
        write_config(tmp_path, payload)

    def test_a_variant_group_derives_its_options_from_the_image_closure(self, tmp_path: Path) -> None:
        self._write_group_config(tmp_path, {"gpu": {"workloads": ["gpu.yaml"]}})

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        group = config.services["gpu"]
        assert isinstance(group, ServiceGroup)
        assert {name: option.vars for name, option in group.options.items()} == {
            "cuda": {"TORCH_DEVICE": "cuda"},
            "rocm": {"TORCH_DEVICE": "rocm"},
        }
        assert all(option.workloads == ["gpu.yaml"] for option in group.options.values())
        assert config.variant_axes == {"TORCH_DEVICE": ["cuda", "rocm"]}

    def test_a_variant_group_whose_closure_declares_no_axis_refuses(self, tmp_path: Path) -> None:
        self._write_gpu_tree(tmp_path, with_axis=False)
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/example/repo",
                "platforms": ["linux/amd64"],
                "base_images": {"TOOLS_IMAGE": "docker.io/library/tools:1"},
                "services": {"gpu": {"workloads": ["gpu.yaml"]}},
            },
        )

        with pytest.raises(ValueError, match=r"services\.gpu: the image closure.*declares no axis"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_variant_group_declaring_two_axes_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "gpu", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n")
        write_dockerfile(tmp_path, "neural-networks-base", "ARG TOOLS_IMAGE\nARG TORCH_DEVICE\nFROM ${TOOLS_IMAGE}\n")
        write_dockerfile(tmp_path, "accel", "ARG ACCEL_BASE\nFROM ${ACCEL_BASE}\n")
        write_dockerfile(tmp_path, "accel-base", "ARG TOOLS_IMAGE\nARG ACCEL_TYPE\nFROM ${TOOLS_IMAGE}\n")
        score = tmp_path / "stack" / "score"
        score.mkdir(parents=True, exist_ok=True)
        for stem in ("gpu", "accel"):
            (score / f"{stem}.yaml").write_text(
                "metadata:\n"
                f"  name: {stem}\n"
                "  annotations:\n"
                f"    outernet.internal/dockerfile: {stem}=workloads/{stem}/Dockerfile\n"
                "containers:\n"
                f"  {stem}: {{}}\n",
                encoding="utf-8",
            )
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/example/repo",
                "platforms": ["linux/amd64"],
                "base_images": {
                    "TOOLS_IMAGE": "docker.io/library/tools:1",
                    "NEURAL_NETWORKS_BASE": {
                        "dockerfile": "workloads/neural-networks-base/Dockerfile",
                        "axis": {"TORCH_DEVICE": ["cuda", "rocm"]},
                    },
                    "ACCEL_BASE": {
                        "dockerfile": "workloads/accel-base/Dockerfile",
                        "axis": {"ACCEL_TYPE": ["fpga"]},
                    },
                },
                "services": {"gpu": {"workloads": ["gpu.yaml", "accel.yaml"]}},
            },
        )

        with pytest.raises(ValueError, match=r"services\.gpu: the image closure.*more than one axis"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_fan_group_and_an_authored_group_declared_together_refuse(self, tmp_path: Path) -> None:
        self._write_group_config(
            tmp_path,
            {"gpu": {"options": {"cuda": {"workloads": ["gpu.yaml"]}}, "workloads": ["gpu.yaml"]}},
        )

        with pytest.raises(ValidationError, match="a group declares exactly one of options: or workloads:"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_derived_option_name_colliding_with_a_member_refuses(self, tmp_path: Path) -> None:
        self._write_group_config(
            tmp_path,
            {
                "gpu": {"workloads": ["gpu.yaml"]},
                "gpu-cuda": {"ref": "oci://ghcr.io/org/repo/gpu-cuda:0.1.0"},
            },
        )

        with pytest.raises(
            ValueError,
            match=r"services\.gpu-cuda: gpu-cuda is already claimed at services\.gpu\.options\.cuda",
        ):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_an_option_var_naming_an_undeclared_variant_refuses(self, tmp_path: Path) -> None:
        self._write_group_config(
            tmp_path,
            {
                "gpu": {"workloads": ["gpu.yaml"]},
                "edge": {"options": {"fast": {"workloads": ["gpu.yaml"], "vars": {"TORCH_DEVICE": "metal"}}}},
            },
        )

        with pytest.raises(
            ValueError,
            match=r"services\.edge\.options\.fast\.vars: TORCH_DEVICE='metal' names no declared variant",
        ):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_k8s_vars_naming_an_undeclared_variant_refuses(self, tmp_path: Path) -> None:
        self._write_group_config(
            tmp_path,
            {"gpu": {"workloads": ["gpu.yaml"]}},
            extra={"k8s": {"workloads": ["gpu.yaml"], "vars": {"TORCH_DEVICE": "metal"}}},
        )

        with pytest.raises(ValueError, match=r"k8s\.vars: TORCH_DEVICE='metal' names no declared variant"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_declared_axes_reach_the_derived_images(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG TOOLS_IMAGE\nARG TORCH_DEVICE\nFROM ${TOOLS_IMAGE}\n")
        write_dockerfile(tmp_path, "localizer", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n")
        score = tmp_path / "stack" / "score"
        score.mkdir(parents=True, exist_ok=True)
        (score / "localizer.yaml").write_text(
            "metadata:\n"
            "  name: localizer\n"
            "  annotations:\n"
            "    outernet.internal/dockerfile: localizer=workloads/localizer/Dockerfile\n"
            "containers:\n"
            "  localizer: {}\n",
            encoding="utf-8",
        )
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "platforms": ["linux/amd64"],
                "k8s": {"workloads": ["localizer.yaml"]},
                "base_images": {
                    "TOOLS_IMAGE": "docker.io/library/tools:1",
                    "NEURAL_NETWORKS_BASE": {
                        "dockerfile": "workloads/neural-networks-base/Dockerfile",
                        "axis": {"TORCH_DEVICE": ["cuda", "rocm"]},
                    },
                },
            },
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        assert config.images["neural-networks-base"].axis == "TORCH_DEVICE"
        assert config.images["localizer"].axis == "TORCH_DEVICE"
        assert config.images["localizer"].variants == ["cuda", "rocm"]
        assert [target for target, _variant in config.images["localizer"].target_variants] == [
            "localizer-cuda",
            "localizer-rocm",
        ]


class TestBaseImages:
    def test_tag_and_digest_forms_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15", "API_IMAGE": "ghcr.io/org/api@" + DIGEST},
            },
        )
        write_lock(tmp_path)

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        assert config.base_images == {
            "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
            "API_IMAGE": "ghcr.io/org/api@" + DIGEST,
        }

    def test_bare_path_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"LOKI_IMAGE": "docker.io/grafana/loki"}})

        with pytest.raises(
            ValidationError, match=r"LOKI_IMAGE: 'docker\.io/grafana/loki' must carry a tag or a digest"
        ):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_env_slot_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"LOKI_IMAGE": "${LOKI_REF}"}})

        with pytest.raises(ValueError, match=r"boatyard\.yml carries no \$\{\.\.\.\} spelling"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_lowercase_key_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"uv_image": "ghcr.io/astral-sh/uv:0.12.15"}})

        with pytest.raises(ValidationError, match="ALL_CAPS"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_mirror_prefixed_declaration_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"UV_IMAGE": "ghcr.io/outernet-foundation/mirror/ghcr.io/astral-sh/uv:0.12.15"},
            },
        )

        with pytest.raises(
            ValidationError, match=r"must not carry the mirror prefix .* \(declarations are upstream-native\)"
        ):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestBaseImageTemplates:
    def _write_toolchain_pins(self, tmp_path: Path, required_version: str = "==0.12.15", python: str = "3.13") -> None:
        (tmp_path / "pyproject.toml").write_text(
            f'[project]\nname = "example"\n\n[tool.uv]\nrequired-version = "{required_version}"\n',
            encoding="utf-8",
        )
        (tmp_path / ".python-version").write_text(python + "\n", encoding="utf-8")

    def test_fixture_pins_resolve(self, tmp_path: Path) -> None:
        self._write_toolchain_pins(tmp_path)
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {
                    "UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:{uv}-python{python}-trixie-slim",
                    "PYTHON_BASE_IMAGE": "docker.io/library/python:{python}-slim",
                },
            },
        )
        write_lock(tmp_path)

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        assert config.base_images == {
            "UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15-python3.13-trixie-slim",
            "PYTHON_BASE_IMAGE": "docker.io/library/python:3.13-slim",
        }

    def test_a_key_may_appear_twice_in_one_value(self, tmp_path: Path) -> None:
        self._write_toolchain_pins(tmp_path)
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"API_IMAGE": "ghcr.io/org/api:{uv}-and-{uv}"},
            },
        )
        write_lock(tmp_path)

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.base_images == {"API_IMAGE": "ghcr.io/org/api:0.12.15-and-0.12.15"}

    def test_plain_values_load_without_any_pin_sources(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"GOLANG_IMAGE": "docker.io/library/golang:1.22-alpine"},
            },
        )
        write_lock(tmp_path)

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.base_images == {"GOLANG_IMAGE": "docker.io/library/golang:1.22-alpine"}

    def test_plain_value_beside_a_templated_one(self, tmp_path: Path) -> None:
        self._write_toolchain_pins(tmp_path)
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {
                    "UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:{uv}-python{python}-trixie-slim",
                    "GOLANG_IMAGE": "docker.io/library/golang:1.22-alpine",
                },
            },
        )
        write_lock(tmp_path)

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        assert config.base_images["GOLANG_IMAGE"] == "docker.io/library/golang:1.22-alpine"

    def test_range_pin_refuses_naming_the_fix(self, tmp_path: Path) -> None:
        self._write_toolchain_pins(tmp_path, required_version=">=0.12")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:{uv}-python{python}-trixie-slim"},
            },
        )
        write_lock(tmp_path)

        with pytest.raises(ValueError, match=r"exact == pin like '==0\.12\.15'"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_wildcard_pin_refuses(self, tmp_path: Path) -> None:
        self._write_toolchain_pins(tmp_path, required_version="==0.12.*")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:{uv}-trixie-slim"},
            },
        )
        write_lock(tmp_path)

        with pytest.raises(ValueError, match="must be an exact == pin"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_missing_uv_pin_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\n', encoding="utf-8")
        (tmp_path / ".python-version").write_text("3.13\n", encoding="utf-8")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:{uv}-trixie-slim"},
            },
        )
        write_lock(tmp_path)

        with pytest.raises(ValueError, match=r"tool\.uv\.required-version is not declared"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_absent_python_version_file_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "example"\n\n[tool.uv]\nrequired-version = "==0.12.15"\n', encoding="utf-8"
        )
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"PYTHON_BASE_IMAGE": "docker.io/library/python:{python}-slim"},
            },
        )
        write_lock(tmp_path)

        with pytest.raises(ValueError, match=r"the \{python\} template requires \.python-version"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_empty_python_version_file_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "example"\n\n[tool.uv]\nrequired-version = "==0.12.15"\n', encoding="utf-8"
        )
        (tmp_path / ".python-version").write_text("\n", encoding="utf-8")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"PYTHON_BASE_IMAGE": "docker.io/library/python:{python}-slim"},
            },
        )
        write_lock(tmp_path)

        with pytest.raises(ValueError, match=r"\.python-version names no version for the \{python\} template"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_unknown_template_key_refuses_listing_the_vocabulary(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"TOOLS_IMAGE": "docker.io/org/tools:{golang}"},
            },
        )
        write_lock(tmp_path)

        with pytest.raises(ValueError, match=r"template key \{golang\} is neither \{uv\} nor \{python\}"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestCharts:
    def test_tag_form_loads(self, tmp_path: Path) -> None:
        write_workload(tmp_path, "api.yaml")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {"workloads": ["api.yaml"], "charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}},
            },
        )
        write_lock(tmp_path)
        (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("loki: {}\n", encoding="utf-8")

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.k8s is not None
        assert config.k8s.charts == {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}

    def test_digest_form_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki@" + DIGEST}}},
        )
        write_lock(tmp_path)
        (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("loki: {}\n", encoding="utf-8")

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.k8s is not None
        assert config.k8s.charts == {"loki": "oci://ghcr.io/grafana/helm-charts/loki@" + DIGEST}

    def test_bare_path_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path, {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki"}}}
        )

        with pytest.raises(ValidationError, match="tag or a digest"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_non_oci_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path, {"requires": ">=0.1", "k8s": {"charts": {"loki": "ghcr.io/grafana/helm-charts/loki:7.3.0"}}}
        )

        with pytest.raises(ValidationError, match="upstream-native OCI"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_mirror_prefixed_declaration_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {
                    "charts": {
                        "loki": "oci://ghcr.io/outernet-foundation/mirror/ghcr.io/grafana/helm-charts/loki:7.3.0"
                    }
                },
            },
        )

        with pytest.raises(
            ValidationError, match=r"must not carry the mirror prefix .* \(declarations are upstream-native\)"
        ):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_env_slot_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:${LOKI_VERSION}"}},
            },
        )

        with pytest.raises(ValueError, match=r"boatyard\.yml carries no \$\{\.\.\.\} spelling"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_missing_values_file_refuses_at_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}}},
        )
        write_lock(tmp_path)

        with pytest.raises(ValueError, match=r"the chart loki requires .*values/loki\.yaml"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestK8sSection:
    def test_workloads_vars_and_charts_load(self, tmp_path: Path) -> None:
        write_workload(tmp_path, "api.yaml")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
            },
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.k8s is not None
        assert config.k8s.workloads == ["api.yaml"]
        assert config.k8s.vars == {"GPU_VARIANT": "cuda"}

    def test_null_section_reads_as_absent(self, tmp_path: Path) -> None:
        (tmp_path / "boatyard.yml").write_text(
            'requires: ">=0.1"\nproject_name: example\nservices:\n  ext:\n    ref: oci://ghcr.io/org/ext:0.1.0\nk8s:\n',
            encoding="utf-8",
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.k8s is None

    def test_empty_section_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "boatyard.yml").write_text('requires: ">=0.1"\nk8s: {}\n', encoding="utf-8")

        with pytest.raises(ValidationError, match="must declare workloads or charts"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_vars_only_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "k8s": {"vars": {"GPU_VARIANT": "cuda"}}})

        with pytest.raises(ValidationError, match="must declare workloads or charts"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_empty_vars_values_refuse(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "k8s": {"workloads": ["api.yaml"], "vars": {"FOO": ""}}})

        with pytest.raises(ValidationError, match="vars values must be non-empty: FOO"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestServices:
    def test_services_is_required(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {},
            },
        )

        with pytest.raises(ValueError, match="services is required"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_empty_vars_values_refuse(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"core": {"options": {"main": {"workloads": ["api.yaml"], "vars": {"FOO": ""}}}}},
            },
        )

        with pytest.raises(ValidationError, match="vars values must be non-empty: FOO"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_groups_options_and_bare_entries_load(self, tmp_path: Path) -> None:
        write_workload(tmp_path, "api.yaml")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "gateway_port": 58080,
                "platforms": EDGE_PLATFORMS,
                "services": {
                    "auth": {
                        "options": {
                            "keycloak": {
                                "ref": "oci://ghcr.io/org/repo/keycloak:0.3.1",
                                "routes": [{"path": "/auth/*", "upstream": "keycloak:8080", "strip_prefix": True}],
                            }
                        },
                    },
                    "placeframe": {
                        "options": {
                            "cuda": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
                            "rocm": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "rocm"}},
                        },
                        "routes": [{"path": "/*", "upstream": "api:8000"}],
                    },
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/livekit:" + SHA_TAG,
                        "routes": [{"path": "/livekit-token/*", "upstream": "livekit-token:8000"}],
                        "ports": [
                            {
                                "service": "livekit",
                                "published": 6178,
                                "target": 6178,
                                "protocol": "tcp",
                            }
                        ],
                    },
                },
            },
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        placeframe = config.services["placeframe"]
        assert isinstance(placeframe, ServiceGroup)
        assert list(placeframe.options) == ["cuda", "rocm"]
        livekit = config.services["livekit"]
        assert isinstance(livekit, ServiceEntry)
        assert livekit.remote
        assert livekit.ports[0].service == "livekit"

    def test_an_entry_carrying_both_sources_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "services": {
                    "both": {
                        "options": {
                            "mixed": {
                                "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                                "workloads": ["api.yaml"],
                            }
                        }
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="ref: or workloads:, not both"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_an_entry_carrying_neither_source_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "services": {"neither": {"routes": [{"path": "/*", "upstream": "api:8000"}]}},
            },
        )

        with pytest.raises(ValidationError, match="ref: or workloads: is required"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_group_shape_with_entry_keys_reports_the_group_branch(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "services": {
                    "tunnel": {
                        "options": {"ngrok": {"workloads": ["tunnel-ngrok.yaml"]}},
                        "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_vars_beside_a_ref_refuse(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {
                    "remote": {
                        "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                        "vars": {"GPU_VARIANT": "cuda"},
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="vars is not allowed with ref:"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_digest_spelled_ref_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"remote": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST}},
            },
        )

        with pytest.raises(ValidationError, match=r"ref .* must carry a published tag, not a digest"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_latest_ref_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"remote": {"ref": "oci://ghcr.io/org/repo/x:latest"}},
            },
        )

        with pytest.raises(ValidationError, match="must not carry the mutable tag 'latest'"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    @pytest.mark.parametrize("tag", [">=0.3.1", "0.3.x", "0.3.X", "0.3.*", "~0.3", "^0.3"])
    def test_a_range_spelled_ref_refuses(self, tag: str, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"remote": {"ref": f"oci://ghcr.io/org/repo/x:{tag}"}},
            },
        )

        with pytest.raises(ValidationError, match="must be an exact tag, not a range"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_tagless_ref_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"remote": {"ref": "oci://ghcr.io/org/repo/x"}},
            },
        )

        with pytest.raises(ValidationError, match="must carry a tag"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_non_oci_ref_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"remote": {"ref": "ghcr.io/org/repo/x:0.3.1"}},
            },
        )

        with pytest.raises(ValidationError, match="must be an OCI include"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    @pytest.mark.parametrize("tag", [SHA_TAG, "0.3.1", "0.3.1-dev.42", "main"])
    def test_every_published_channel_spelling_loads(self, tag: str, tmp_path: Path) -> None:
        write_lock(tmp_path, entries={}, bundles={"REMOTE_BUNDLE": {"reference": f"ghcr.io/org/repo/x:{tag}@{DIGEST}"}})
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"remote": {"ref": f"oci://ghcr.io/org/repo/x:{tag}"}},
            },
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None

    def test_an_empty_workloads_list_refuses(self) -> None:
        with pytest.raises(ValidationError, match="a group declares exactly one of options: or workloads:"):
            ConfigFile.model_validate({
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "services": {"empty": {"workloads": []}},
            })

    def test_a_config_without_project_name_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "boatyard.yml").write_text("requires: '>=0.1'\n", encoding="utf-8")

        with pytest.raises(ValidationError, match="project_name"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_local_entries_require_repository(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway_port": 8080,
                "services": {"core": {"workloads": ["api.yaml"]}},
            },
        )

        with pytest.raises(ValidationError, match="repository is required"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_remote_only_services_need_no_repository(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway_port": 8080,
                "platforms": EDGE_PLATFORMS,
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/livekit:" + SHA_TAG,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        assert ConfigFile.load(tmp_path / "boatyard.yml") is not None


class TestQualifiedVocabulary:
    def _config(self, services: dict[str, object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "repository": "ghcr.io/org/repo",
            "project_name": "example",
            "platforms": ["linux/amd64"],
            "services": services,
        }

    def test_the_same_option_name_in_two_groups_loads_qualified(self, tmp_path: Path) -> None:
        write_workload(tmp_path, "api.yaml")
        write_workload(tmp_path, "edge.yaml")
        write_config(
            tmp_path,
            self._config({
                "one": {"options": {"cuda": {"workloads": ["api.yaml"]}}},
                "two": {"options": {"cuda": {"workloads": ["edge.yaml"]}}},
            }),
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        assert [name for name, _entry in config.local_service_entries()] == ["one-cuda", "two-cuda"]

    def test_an_option_and_a_bare_entry_sharing_a_name_load(self, tmp_path: Path) -> None:
        write_lock(tmp_path, bundles=bundle_entries("livekit", "tunnel-livekit"))
        write_config(
            tmp_path,
            self._config({
                "livekit": {"ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG},
                "tunnel": {"options": {"livekit": {"ref": "oci://ghcr.io/org/repo/y:" + SHA_TAG}}},
            }),
        )

        assert ConfigFile.load(tmp_path / "boatyard.yml") is not None

    def test_two_sites_producing_one_qualified_spelling_refuse(self, tmp_path: Path) -> None:
        write_workload(tmp_path, "api.yaml")
        write_config(
            tmp_path,
            self._config({
                "a": {"options": {"b-c": {"workloads": ["api.yaml"]}}},
                "a-b": {"options": {"c": {"workloads": ["api.yaml"]}}},
            }),
        )

        with pytest.raises(ValueError, match=r"a-b-c is already claimed at"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_bare_entry_matching_a_qualified_spelling_refuses(self, tmp_path: Path) -> None:
        write_workload(tmp_path, "api.yaml")
        write_config(
            tmp_path,
            self._config({
                "tunnel-ngrok": {"ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG},
                "tunnel": {"options": {"ngrok": {"workloads": ["api.yaml"]}}},
            }),
        )

        with pytest.raises(ValueError, match=r"tunnel-ngrok is already claimed at"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestAuthGroup:
    def _auth_config(self, options: dict[str, object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "project_name": "example",
            "gateway_port": 8080,
            "platforms": EDGE_PLATFORMS,
            "services": {"auth": {"options": options}},
        }

    def test_an_auth_option_with_exactly_one_route_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._auth_config({
                "keycloak": {
                    "ref": "oci://ghcr.io/org/repo/keycloak:0.3.1",
                    "routes": [{"path": "/auth/*", "upstream": "keycloak:8080", "strip_prefix": True}],
                }
            }),
        )

        assert ConfigFile.load(tmp_path / "boatyard.yml") is not None

    def test_an_auth_option_without_an_authored_route_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._auth_config({"keycloak": {"ref": "oci://ghcr.io/org/repo/keycloak:0.3.1"}}),
        )

        assert ConfigFile.load(tmp_path / "boatyard.yml") is not None

    def test_an_auth_option_with_two_routes_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._auth_config({
                "keycloak": {
                    "ref": "oci://ghcr.io/org/repo/keycloak:0.3.1",
                    "routes": [
                        {"path": "/auth/*", "upstream": "keycloak:8080"},
                        {"path": "/auth2/*", "upstream": "keycloak:8080"},
                    ],
                }
            }),
        )

        with pytest.raises(ValidationError, match="at most one route"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_bare_entry_named_auth_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"auth": {"ref": "oci://ghcr.io/org/repo/keycloak:0.3.1"}},
            },
        )

        with pytest.raises(ValidationError, match=r"services\.auth must be a group \(the name is reserved\)"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestWorkloadLensAnnotations:
    def _document(self, annotations: dict[str, Yaml]) -> WorkloadDocument:
        raw: dict[str, Yaml] = {"metadata": {"name": "api", "annotations": annotations}, "containers": {}}
        return WorkloadDocument.parse(raw, "api.yaml")

    def test_multiple_routes_parse_in_order(self) -> None:
        document = self._document({"outernet.internal/route": "/grafana/*=grafana:3000,/*=api:8000"})

        assert document.routes == [
            ("/grafana/*", "grafana:3000", False),
            ("/*", "api:8000", False),
        ]

    def test_strip_attaches_to_the_preceding_route(self) -> None:
        document = self._document({"outernet.internal/route": "/auth/*=keycloak:8080,strip"})

        assert document.routes == [("/auth/*", "keycloak:8080", True)]

    def test_a_bad_protocol_refuses(self) -> None:
        with pytest.raises(ValidationError, match=r"protocol 'sctp' must be one of"):
            self._document({"outernet.internal/publish": "6178:6178/sctp"})

    def test_an_out_of_range_port_refuses(self) -> None:
        with pytest.raises(ValidationError, match=r"port '65536' outside 1-65535"):
            self._document({"outernet.internal/publish": "6178:65536"})

    def test_strip_without_a_wildcard_path_refuses(self) -> None:
        with pytest.raises(ValidationError, match=r"strip requires the path to end in /\*"):
            self._document({"outernet.internal/route": "/auth=keycloak:8080,strip"})

    def test_strip_as_the_first_token_refuses(self) -> None:
        with pytest.raises(ValidationError, match=r"'strip' attaches to no preceding route"):
            self._document({"outernet.internal/route": "strip"})

    def test_padded_and_empty_entries_refuse(self) -> None:
        for value in ("", " /x/*=x:1", "/x/*=x:1,"):
            with pytest.raises(ValidationError, match="non-empty and unpadded"):
                self._document({"outernet.internal/route": value})

    def test_inner_padding_refuses_on_the_grammar(self) -> None:
        with pytest.raises(ValidationError, match=r"must be a service name and a literal port"):
            self._document({"outernet.internal/route": "/x/*= x:1"})

    def test_a_malformed_route_path_refuses(self) -> None:
        with pytest.raises(ValidationError, match=r"must be a /-rooted segment chain"):
            self._document({"outernet.internal/route": "grafana/*=grafana:3000"})

    def test_a_malformed_route_upstream_refuses(self) -> None:
        with pytest.raises(ValidationError, match=r"must be a service name and a literal port"):
            self._document({"outernet.internal/route": "/*=grafana:three"})

    def test_publishes_parse_with_defaults_and_protocols(self) -> None:
        document = self._document({"outernet.internal/publish": "6178:6178,5957:5957/udp"})

        assert document.publishes == [
            (6178, 6178, "tcp"),
            (5957, 5957, "udp"),
        ]

    def test_a_pairless_publish_entry_refuses(self) -> None:
        with pytest.raises(ValidationError, match=r"must be a published:target pair"):
            self._document({"outernet.internal/publish": "6178"})


class TestRoutes:
    def _routed(self, routes: list[object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "project_name": "example",
            "gateway_port": 8080,
            "platforms": EDGE_PLATFORMS,
            "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG, "routes": routes}},
        }

    def test_edge_wide_path_uniqueness_refuses_duplicates(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway_port": 8080,
                "services": {
                    "one": {
                        "options": {
                            "a": {
                                "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                                "routes": [{"path": "/x/*", "upstream": "x:1"}],
                            }
                        },
                    },
                    "two": {
                        "options": {
                            "b": {
                                "ref": "oci://ghcr.io/org/repo/y:" + SHA_TAG,
                                "routes": [{"path": "/x/*", "upstream": "y:1"}],
                            }
                        },
                    },
                },
            },
        )

        with pytest.raises(ValidationError, match=r"route path /x/\* is already declared at services\.one\.options\.a"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    @pytest.mark.parametrize(
        "path",
        ["grafana/*", "//grafana/*", "/grafana//x", "/*/*", "/grafana /x", "grafana", "/grafana*", "/*grafana"],
    )
    def test_malformed_paths_refuse(self, path: str, tmp_path: Path) -> None:
        write_config(tmp_path, self._routed([{"path": path, "upstream": "api:8000"}]))

        with pytest.raises(ValidationError, match="must be a /-rooted segment chain"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_plain_and_wildcard_paths_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._routed([
                {"path": "/*", "upstream": "api:8000"},
                {"path": "/livekit-token/*", "upstream": "livekit-token:8000"},
            ]),
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None

    def test_an_upstream_without_a_literal_port_refuses(self, tmp_path: Path) -> None:
        write_config(tmp_path, self._routed([{"path": "/*", "upstream": "api:eight"}]))

        with pytest.raises(ValidationError, match="a service name and a literal port"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_strip_prefix_on_a_non_wildcard_path_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._routed([{"path": "/grafana", "upstream": "grafana:3000", "strip_prefix": True}]),
        )

        with pytest.raises(ValidationError, match="must end in /\\*"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_raw_route_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._routed([{"path": "/x/*", "upstream": "x:1", "raw": 'respond "hi" 200'}]),
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None

    def test_routes_without_a_gateway_refuse(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                        "routes": [{"path": "/*", "upstream": "x:1"}],
                    },
                },
            },
        )

        with pytest.raises(ValidationError, match="routes require a gateway declaration"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_group_level_routes_without_a_gateway_refuse_before_the_fan(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "repository": "ghcr.io/org/example",
                "platforms": ["linux/amd64"],
                "services": {"livekit": {"workloads": ["api.yaml"], "routes": [{"path": "/*", "upstream": "x:1"}]}},
            },
        )

        with pytest.raises(ValidationError, match="routes require a gateway declaration"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestGatewayKey:
    def test_the_barrier_and_edge_names_refuse_in_base_images(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "platforms": ["linux/amd64"],
                "base_images": {"CADDY_IMAGE": "docker.io/library/caddy:2.8.4-alpine"},
                "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG}},
            },
        )

        with pytest.raises(ValidationError, match="barrier and edge pins are boatyard-owned"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_the_owned_pins_carry_the_tool_pin_shape(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "platforms": ["linux/amd64"],
                "barrier_image": "docker.io/library/alpine",
                "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG}},
            },
        )

        with pytest.raises(
            ValidationError, match=r"barrier_image: 'docker\.io/library/alpine' must carry a tag or a digest"
        ):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_gateway_owns_the_edge_pin_and_overrides_ship_theirs(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "platforms": ["linux/amd64"],
                "gateway_port": 8080,
                "edge_image": "docker.io/library/caddy:2.9.0",
                "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG}},
            },
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        assert config.owned_images == {"CADDY_IMAGE": "docker.io/library/caddy:2.9.0"}

    def test_a_routeless_gateway_loads(self, tmp_path: Path) -> None:
        write_lock(tmp_path, bundles=bundle_entries("livekit"))
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "platforms": ["linux/amd64"],
                "gateway_port": 8080,
                "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG}},
            },
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None

    def test_a_literal_port_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway_port": 8443,
                "platforms": EDGE_PLATFORMS,
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        assert ConfigFile.load(tmp_path / "boatyard.yml") is not None

    def test_a_malformed_gateway_value_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway_port": "${gateway port}",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        with pytest.raises(ValueError, match=r"boatyard\.yml carries no \$\{\.\.\.\} spelling"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestPorts:
    def _ports_config(self, services: dict[str, object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "repository": "ghcr.io/org/repo",
            "project_name": "example",
            "gateway_port": 8080,
            "platforms": EDGE_PLATFORMS,
            "services": services,
        }

    def test_the_mapping_form_defaults_protocol_to_tcp(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": [
                        {"service": "livekit", "published": 6178, "target": 6178},
                        {"service": "livekit", "published": 6179, "target": 6179, "protocol": "udp"},
                    ],
                }
            }),
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        livekit = config.services["livekit"]
        assert isinstance(livekit, ServiceEntry)
        assert [(port.service, port.published, port.target, port.protocol) for port in livekit.ports] == [
            ("livekit", 6178, 6178, "tcp"),
            ("livekit", 6179, 6179, "udp"),
        ]

    def test_an_int_port_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": [{"service": "livekit", "published": 6179, "target": 6179}],
                }
            }),
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        livekit = config.services["livekit"]
        assert isinstance(livekit, ServiceEntry)
        assert [(port.published, port.target) for port in livekit.ports] == [(6179, 6179)]

    def test_an_out_of_range_int_port_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": [{"service": "livekit", "published": 65536, "target": 6179}],
                }
            }),
        )

        with pytest.raises(ValidationError, match="outside the port range"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_string_port_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": ["6179/6179/udp"],
                }
            }),
        )
        write_lock(tmp_path)

        with pytest.raises(ValidationError, match="Input should be a valid dictionary or instance of PortDeclaration"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_an_empty_group_options_map_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "tunnel": {"options": None, "ports": [{"service": "ngrok", "published": 4040, "target": 4040}]},
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                },
            }),
        )
        write_lock(tmp_path)

        with pytest.raises(ValidationError):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_the_mapping_form_spells_the_owning_service_and_host_ip(self, tmp_path: Path) -> None:
        write_workload(tmp_path, "api.yaml")
        write_config(
            tmp_path,
            self._ports_config({
                "placeframe": {
                    "options": {"cuda": {"workloads": ["api.yaml"]}},
                    "ports": [
                        {
                            "service": "api",
                            "host_ip": "127.0.0.1",
                            "published": 5679,
                            "target": 5678,
                        }
                    ],
                },
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                },
            }),
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None

    def test_a_non_integer_port_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": [{"service": "livekit", "published": "eight", "target": 5678}],
                }
            }),
        )

        with pytest.raises(ValidationError, match="Input should be a valid integer"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_digit_string_port_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x:" + SHA_TAG,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": [{"service": "livekit", "published": "6179", "target": 6179}],
                }
            }),
        )

        with pytest.raises(ValidationError, match="Input should be a valid integer"):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestRepository:
    def test_repository_without_host_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "repository": "lbe-toolkit"})

        with pytest.raises(ValidationError, match="registry-host/repository-path"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_trailing_slash_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "repository": "ghcr.io/org/repo/"})

        with pytest.raises(ValidationError, match="trailing slash"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_repository_without_derived_images_loads(self, tmp_path: Path) -> None:
        # The publish shape for a repo whose whole runtime is its own workload set: no
        # Dockerfile image builds, repository naming the artifact registry.
        write_workload(tmp_path, "rathole-client.yaml")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "platforms": ["linux/amd64"],
                "services": {"rathole-client": {"options": {"client": {"workloads": ["rathole-client.yaml"]}}}},
            },
        )
        write_lock(tmp_path)

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.repository == "ghcr.io/org/repo"
        assert config.images == {}


class TestPlatforms:
    def test_arch_pairs_load(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "platforms": ["linux/arm64"]})

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None and config.platforms == ["linux/arm64"]

    def test_garbage_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "platforms": ["arm64"]})

        with pytest.raises(ValidationError, match="os/arch pair"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_base_images_without_platforms_refuse_naming_the_fix(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"}})

        with pytest.raises(ValidationError, match=r"platforms is required.*\[linux/amd64\]"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_charts_stay_exempt_from_the_platforms_rule(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}}},
        )
        write_lock(tmp_path)
        (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("loki: {}\n", encoding="utf-8")

        assert ConfigFile.load(tmp_path / "boatyard.yml") is not None


class TestLockPairing:
    def test_base_images_without_lock_load_for_bootstrap(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            },
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config.base_images == {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"}

    def test_charts_without_lock_load_for_bootstrap(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}}},
        )
        (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("loki: {}\n", encoding="utf-8")

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config.declared_charts == {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}

    def test_lock_beside_base_images_loads(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            },
        )
        write_lock(tmp_path)

        assert ConfigFile.load(tmp_path / "boatyard.yml") is not None

    def test_refs_without_lock_load_for_bootstrap(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "services": {"livekit": {"ref": "oci://ghcr.io/x/y:" + SHA_TAG}},
                "project_name": "example",
            },
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert [name for name, _declared, _entry in config.remote_declared_entries()] == ["livekit"]

    def test_a_local_repo_loads_without_a_lock_file(self, tmp_path: Path) -> None:
        write_workload(tmp_path, "api.yaml")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "platforms": ["linux/amd64"],
                "services": {"core": {"options": {"main": {"workloads": ["api.yaml"]}}}},
                "project_name": "example",
            },
        )

        assert ConfigFile.load(tmp_path / "boatyard.yml") is not None


class TestImageDerivation:
    def test_repository_required_when_images_exist(self, tmp_path: Path) -> None:
        write_dockerfile_workload(tmp_path, "api.yaml")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {"workloads": ["api.yaml"]},
                "platforms": ["linux/amd64"],
                "base_images": {"TOOLS_IMAGE": "docker.io/library/tools:1"},
            },
        )

        with pytest.raises(ValueError, match="repository is required"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_derived_images_attach_to_the_loaded_config(self, tmp_path: Path) -> None:
        write_dockerfile_workload(tmp_path, "api.yaml")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "k8s": {"workloads": ["api.yaml"]},
                "platforms": ["linux/amd64"],
                "base_images": {"TOOLS_IMAGE": "docker.io/library/tools:1"},
            },
        )

        config = ConfigFile.load(tmp_path / "boatyard.yml")

        assert config is not None
        assert sorted(config.images) == ["api"]

    def test_a_lock_annotation_naming_no_remote_base_images_key_refuses(self, tmp_path: Path) -> None:
        score = tmp_path / "stack" / "score"
        score.mkdir(parents=True, exist_ok=True)
        (score / "keycloak.yaml").write_text(
            "metadata:\n"
            "  name: keycloak\n"
            "  annotations:\n"
            "    outernet.internal/lock: keycloak=KEYCLOAK_IMAGE\n"
            "containers:\n"
            "  keycloak: {}\n",
            encoding="utf-8",
        )
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "platforms": ["linux/amd64"],
                "base_images": {
                    "TOOLS_IMAGE": "docker.io/library/tools:1",
                    "NEURAL_NETWORKS_BASE": {"dockerfile": "workloads/does-not-exist/Dockerfile"},
                },
                "services": {"identity": {"workloads": ["keycloak.yaml"]}},
            },
        )

        with pytest.raises(
            ValueError, match=r"keycloak\.yaml container 'keycloak': 'KEYCLOAK_IMAGE' names no remote base_images"
        ):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_a_lock_annotation_naming_a_built_entry_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG TOOLS_IMAGE\nARG TORCH_DEVICE\nFROM ${TOOLS_IMAGE}\n")
        score = tmp_path / "stack" / "score"
        score.mkdir(parents=True, exist_ok=True)
        (score / "gpu.yaml").write_text(
            "metadata:\n"
            "  name: gpu\n"
            "  annotations:\n"
            "    outernet.internal/lock: gpu=NEURAL_NETWORKS_BASE\n"
            "containers:\n"
            "  gpu: {}\n",
            encoding="utf-8",
        )
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "platforms": ["linux/amd64"],
                "base_images": {
                    "TOOLS_IMAGE": "docker.io/library/tools:1",
                    "NEURAL_NETWORKS_BASE": {
                        "dockerfile": "workloads/neural-networks-base/Dockerfile",
                        "axis": {"TORCH_DEVICE": ["cuda"]},
                    },
                },
                "services": {"core": {"workloads": ["gpu.yaml"]}},
            },
        )
        write_dockerfile(tmp_path, "localizer", "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n")

        with pytest.raises(
            ValueError, match=r"gpu\.yaml container 'gpu': 'NEURAL_NETWORKS_BASE' names no remote base_images"
        ):
            ConfigFile.load(tmp_path / "boatyard.yml")


class TestBareServiceNameClaims:
    def _local_config(self, tmp_path: Path, workloads: list[str]) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "platforms": ["linux/amd64"],
                "services": {"core": {"workloads": workloads}},
            },
        )

    def test_a_missing_workload_file_refuses(self, tmp_path: Path) -> None:
        self._local_config(tmp_path, ["api.yaml"])

        with pytest.raises(ValueError, match="the workload file is required"):
            ConfigFile.load(tmp_path / "boatyard.yml")

    def test_the_same_container_name_across_entries_loads(self, tmp_path: Path) -> None:
        write_workload(tmp_path, "alpha.yaml")
        write_workload(tmp_path, "beta.yaml")
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "platforms": ["linux/amd64"],
                "services": {
                    "one": {"options": {"alpha": {"workloads": ["alpha.yaml"]}}},
                    "two": {"options": {"beta": {"workloads": ["beta.yaml"]}}},
                },
            },
        )

        assert ConfigFile.load(tmp_path / "boatyard.yml") is not None


def test_direct_model_construction_is_valid_for_tests(tmp_path: Path) -> None:
    config = ConfigFile.model_validate({
        "requires": ">=0.1",
        "repository": "ghcr.io/org/repo",
        "project_name": "example",
        "gateway_port": 8080,
        "platforms": ["linux/amd64"],
        "services": {"core": {"options": {"main": {"workloads": ["api.yaml"]}}}},
    })
    config.root = tmp_path

    group = config.services["core"]
    assert isinstance(group, ServiceGroup)
    assert not group.options["main"].remote
    assert group.options["main"].workloads == ["api.yaml"]


def test_local_service_entries_flatten_in_declaration_order_skipping_refs(tmp_path: Path) -> None:
    write_workload(tmp_path, "api.yaml")
    write_workload(tmp_path, "solo.yaml")
    write_lock(tmp_path, bundles=bundle_entries("identity-keycloak", "livekit"))
    write_config(
        tmp_path,
        {
            "requires": ">=0.1",
            "repository": "ghcr.io/org/repo",
            "project_name": "example",
            "platforms": ["linux/amd64"],
            "services": {
                "identity": {"options": {"keycloak": {"ref": "oci://ghcr.io/org/repo/keycloak:0.3.1"}}},
                "placeframe": {
                    "options": {
                        "cuda": {"workloads": ["api.yaml"]},
                        "rocm": {"workloads": ["api.yaml"]},
                    },
                },
                "livekit": {"ref": "oci://ghcr.io/org/repo/livekit:" + SHA_TAG},
                "solo": {"options": {"main": {"workloads": ["solo.yaml"]}}},
            },
        },
    )

    config = ConfigFile.load(tmp_path / "boatyard.yml")

    assert config is not None
    assert [(name, entry.workloads) for name, entry in config.local_service_entries()] == [
        ("placeframe-cuda", ["api.yaml"]),
        ("placeframe-rocm", ["api.yaml"]),
        ("solo-main", ["solo.yaml"]),
    ]


def _write_raw_lock(tmp_path: Path, text: str) -> Path:
    lock = tmp_path / LOCK_PATH
    lock.write_text(text, encoding="utf-8")
    return lock


class TestConfigLockParse:
    def _parse(self, tmp_path: Path) -> LockFile:
        return load_yaml_model(LockFile, tmp_path / LOCK_PATH)

    def test_the_empty_lock_stays_legal(self, tmp_path: Path) -> None:
        lock = LockFile.model_validate({})

        assert lock.images == {}
        assert lock.charts == {}

    def test_malformed_yaml_refuses_raw(self, tmp_path: Path) -> None:
        _write_raw_lock(tmp_path, "images:\n  A_IMAGE: [unclosed\n")

        with pytest.raises(yaml.YAMLError):
            self._parse(tmp_path)

    def test_duplicate_keys_refuse_naming_the_line(self, tmp_path: Path) -> None:
        lock = _write_raw_lock(
            tmp_path,
            f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\n  A_IMAGE:\n    index: ghcr.io/x/a:2@{DIGEST}\n",
        )

        with pytest.raises(ValueError, match=rf"{re.escape(lock.as_posix())}: line 4: duplicate key 'A_IMAGE'"):
            self._parse(tmp_path)

    def test_the_env_line_format_refuses_as_a_bare_scalar(self, tmp_path: Path) -> None:
        _write_raw_lock(tmp_path, f"A_IMAGE=ghcr.io/x/a:1@{DIGEST}\n")

        with pytest.raises(ValidationError, match="Input should be a valid dictionary"):
            self._parse(tmp_path)

    def test_a_non_digest_pinned_index_refuses(self, tmp_path: Path) -> None:
        _write_raw_lock(tmp_path, "images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1.2.3\n")

        with pytest.raises(ValueError, match="not digest-pinned"):
            self._parse(tmp_path)

    def test_a_non_digest_pinned_platform_entry_refuses(self, tmp_path: Path) -> None:
        _write_raw_lock(
            tmp_path,
            f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\n"
            "    platforms:\n      linux/amd64: ghcr.io/x/a:1\n",
        )

        with pytest.raises(ValueError, match="not digest-pinned"):
            self._parse(tmp_path)

    def test_a_non_digest_pinned_chart_refuses(self, tmp_path: Path) -> None:
        _write_raw_lock(tmp_path, "charts:\n  LOKI_CHART: ghcr.io/x/loki:7.3.0\n")

        with pytest.raises(ValueError, match="chart LOKI_CHART is not digest-pinned"):
            self._parse(tmp_path)

    def test_a_non_digest_pinned_bundle_refuses(self, tmp_path: Path) -> None:
        _write_raw_lock(
            tmp_path,
            "bundles:\n  LIVEKIT_BUNDLE:\n    reference: ghcr.io/x/livekit:0.3.1\n"
            "    services: []\n    routes: []\n    ports: []\n    container_names: []\n",
        )

        with pytest.raises(ValidationError, match="bundle entry is not digest-pinned"):
            self._parse(tmp_path)

    def test_a_malformed_platform_key_refuses(self, tmp_path: Path) -> None:
        _write_raw_lock(
            tmp_path,
            f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\n"
            f"    platforms:\n      amd64: ghcr.io/x/a:1@{DIGEST}\n",
        )

        with pytest.raises(ValueError, match="os/arch pair"):
            self._parse(tmp_path)

    def test_a_name_in_two_sections_refuses(self, tmp_path: Path) -> None:
        _write_raw_lock(
            tmp_path,
            f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\ncharts:\n  A_IMAGE: ghcr.io/x/a:1@{DIGEST}\n",
        )

        with pytest.raises(ValidationError, match=r"at most one of images, charts, bundles"):
            self._parse(tmp_path)

    def test_a_bundle_name_colliding_with_base_images_keys_refuses(self, tmp_path: Path) -> None:
        _write_raw_lock(
            tmp_path,
            f"images:\n  LIVEKIT_BUNDLE:\n    index: ghcr.io/x/a:1@{DIGEST}\n"
            f"bundles:\n  LIVEKIT_BUNDLE:\n    reference: ghcr.io/x/livekit:0.3.1@{DIGEST}\n"
            "    services: []\n    routes: []\n    ports: []\n    container_names: []\n",
        )

        with pytest.raises(ValidationError, match=r"at most one of images, charts, bundles"):
            self._parse(tmp_path)

    def test_require_answers_the_index_for_images_and_the_flat_entry_for_charts(self, tmp_path: Path) -> None:
        _write_raw_lock(
            tmp_path,
            f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\n"
            f"charts:\n  LOKI_CHART: ghcr.io/x/loki:7.3.0@{DIGEST}\n"
            f"bundles:\n  LIVEKIT_BUNDLE:\n    reference: ghcr.io/x/livekit:0.3.1@{DIGEST}\n"
            "    services: []\n    routes: []\n    ports: []\n    container_names: []\n",
        )
        lock = self._parse(tmp_path)

        assert lock.require("A_IMAGE", "subject") == f"ghcr.io/x/a:1@{DIGEST}"
        assert lock.require("LOKI_CHART", "subject") == f"ghcr.io/x/loki:7.3.0@{DIGEST}"
        assert lock.require("LIVEKIT_BUNDLE", "subject") == f"ghcr.io/x/livekit:0.3.1@{DIGEST}"

    def test_require_refuses_naming_the_lock_verb(self, tmp_path: Path) -> None:
        lock = LockFile.model_validate({})

        with pytest.raises(ValueError, match=r"routes are declared: boatyard\.lock\.yml lacks the CADDY_IMAGE entry"):
            lock.require("CADDY_IMAGE", "routes are declared")
