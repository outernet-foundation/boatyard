import importlib.metadata
import re
from typing import Any
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from boatyard.config import (
    DEV_SENTINEL,
    Config,
    ServiceEntry,
)
from boatyard.config_lock import LOCK_PATH, parse_config_lock

DIGEST = "sha256:" + "a" * 64


def write_config(tmp_path: Path, payload: dict[str, object]) -> None:
    (tmp_path / "docker-devkit.yaml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def write_lock(tmp_path: Path, entries: dict[str, str] | None = None) -> None:
    (tmp_path / LOCK_PATH).parent.mkdir(parents=True, exist_ok=True)
    images = entries or {"ALPINE_IMAGE": "ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.21.0@" + DIGEST}
    (tmp_path / LOCK_PATH).write_text(
        yaml.safe_dump({"images": {name: {"index": reference} for name, reference in images.items()}}, sort_keys=False),
        encoding="utf-8",
    )


def write_edge_lock(tmp_path: Path) -> None:
    reference = "ghcr.io/outernet-foundation/mirror/docker.io/library/caddy:2.8.4-alpine@" + DIGEST
    write_lock(tmp_path, {"CADDY_IMAGE": reference})


def write_dockerfile(tmp_path: Path, service: str, text: str, top: str = "workloads") -> None:
    directory = tmp_path / top / service
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Dockerfile").write_text(text, encoding="utf-8")


def test_load_returns_none_when_file_absent(tmp_path: Path) -> None:
    assert Config.load(tmp_path) is None


def _installed_version(version: str) -> Any:
    def _version(_name: str) -> str:
        return version

    return _version


class TestRequires:
    def test_omitted_is_required(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"project_name": "example"})

        with pytest.raises(ValidationError, match="requires"):
            Config.load(tmp_path)

    def test_empty_string_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ""})

        with pytest.raises(ValidationError, match="requires"):
            Config.load(tmp_path)

    def test_invalid_specifier_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": "not-a-specifier"})

        with pytest.raises(ValidationError, match="requires"):
            Config.load(tmp_path)

    def test_dev_sentinel_skips_check(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version(DEV_SENTINEL))
        write_config(tmp_path, {"requires": ">=0.1"})

        config = Config.load(tmp_path)

        assert config is not None and config.requires == ">=0.1"

    def test_in_range_loads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version("0.1.18"))
        write_config(tmp_path, {"requires": ">=0.1.18"})

        config = Config.load(tmp_path)

        assert config is not None and config.requires == ">=0.1.18"

    def test_dev_channel_prerelease_satisfies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version("0.1.19.dev5"))
        write_config(tmp_path, {"requires": ">=0.1.18"})

        config = Config.load(tmp_path)

        assert config is not None

    def test_out_of_range_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(importlib.metadata, "version", _installed_version("0.1.17"))
        write_config(tmp_path, {"requires": ">=0.1.18"})

        with pytest.raises(ValueError, match="does not satisfy requires"):
            Config.load(tmp_path)


def test_load_rejects_duplicate_keys(tmp_path: Path) -> None:
    (tmp_path / "docker-devkit.yaml").write_text('requires: ">=0.1"\nrequires: ">=0.2"\n', encoding="utf-8")

    with pytest.raises(Exception, match=r"Duplicate key"):
        Config.load(tmp_path)


def test_load_rejects_unknown_top_level_keys(tmp_path: Path) -> None:
    write_config(tmp_path, {"requires": ">=0.1", "lifecycle_prefix": "ghcr.io/x/mirror"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Config.load(tmp_path)


class TestOldSectionsDied:
    def test_generate_score_section_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "generate-score": {"project_name": "example"}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Config.load(tmp_path)

    def test_publish_compose_section_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "publish-compose": {"repository": "ghcr.io/org/repo"}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Config.load(tmp_path)

    def test_lifecycle_section_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "lifecycle": {"files": ["compose.yml"]}})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Config.load(tmp_path)

    def test_bundles_key_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "bundles": {"core": {"workloads": ["api.yaml"]}}},
        )

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Config.load(tmp_path)


class TestWorkloadReferences:
    def test_nested_service_workload_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "project_name": "example", "services": {"core": {"workloads": ["nested/api.yaml"]}}},
        )

        with pytest.raises(ValidationError, match=r"flat \.yaml file name"):
            Config.load(tmp_path)

    def test_non_yaml_service_workload_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "project_name": "example", "services": {"core": {"workloads": ["api.yml"]}}},
        )

        with pytest.raises(ValidationError, match=r"flat \.yaml file name"):
            Config.load(tmp_path)

    def test_nested_k8s_workload_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "k8s": {"workloads": ["nested/api.yaml"]}})

        with pytest.raises(ValidationError, match=r"flat \.yaml file name"):
            Config.load(tmp_path)


class TestVariants:
    def test_declared_axes_load(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "variants": {"TORCH_DEVICE": ["cuda", "rocm"]}})

        config = Config.load(tmp_path)

        assert config is not None
        assert config.variants == {"TORCH_DEVICE": ["cuda", "rocm"]}

    def test_lowercase_axis_name_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "variants": {"torch_device": ["cuda"]}})

        with pytest.raises(ValidationError, match="ALL_CAPS env name"):
            Config.load(tmp_path)

    def test_duplicate_values_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "variants": {"TORCH_DEVICE": ["cuda", "cuda"]}})

        with pytest.raises(ValidationError, match="declared twice"):
            Config.load(tmp_path)

    def test_uppercase_value_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "variants": {"TORCH_DEVICE": ["CUDA"]}})

        with pytest.raises(ValidationError, match="bare lowercase spelling"):
            Config.load(tmp_path)

    def test_declared_axes_reach_the_derived_services(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "localizer", "ARG PY_IMAGE\nARG TORCH_DEVICE\nFROM ${PY_IMAGE}\n")
        write_config(
            tmp_path,
            {"requires": ">=0.1", "repository": "ghcr.io/example/repo", "variants": {"TORCH_DEVICE": ["cuda", "rocm"]}},
        )

        config = Config.load(tmp_path)

        assert config is not None
        assert config.service_builds["localizer"].axis == "TORCH_DEVICE"
        assert config.service_builds["localizer"].variants == ["cuda", "rocm"]
        assert [target for target, _variant in config.service_builds["localizer"].target_variants] == [
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

        config = Config.load(tmp_path)

        assert config is not None
        assert config.base_images == {
            "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
            "API_IMAGE": "ghcr.io/org/api@" + DIGEST,
        }

    def test_bare_path_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"ALPINE_IMAGE": "docker.io/library/alpine"}})

        with pytest.raises(ValidationError, match="path:tag"):
            Config.load(tmp_path)

    def test_env_hole_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"ALPINE_IMAGE": "${ALPINE_REF}"}})

        with pytest.raises(ValidationError, match="path:tag"):
            Config.load(tmp_path)

    def test_lowercase_key_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"uv_image": "ghcr.io/astral-sh/uv:0.12.15"}})

        with pytest.raises(ValidationError, match="ALL_CAPS"):
            Config.load(tmp_path)


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

        config = Config.load(tmp_path)

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

        config = Config.load(tmp_path)

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

        config = Config.load(tmp_path)

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

        config = Config.load(tmp_path)

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
            Config.load(tmp_path)

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
            Config.load(tmp_path)

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
            Config.load(tmp_path)

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

        with pytest.raises(ValueError, match=r"\.python-version: the file is absent"):
            Config.load(tmp_path)

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

        with pytest.raises(ValueError, match=r"\.python-version: the file is empty"):
            Config.load(tmp_path)

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

        with pytest.raises(
            ValueError, match=r"template key \{golang\} is unknown; the toolchain pins are \{uv\}, \{python\}"
        ):
            Config.load(tmp_path)


class TestCharts:
    def test_tag_form_loads(self, tmp_path: Path) -> None:
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

        config = Config.load(tmp_path)

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

        config = Config.load(tmp_path)

        assert config is not None and config.k8s is not None
        assert config.k8s.charts == {"loki": "oci://ghcr.io/grafana/helm-charts/loki@" + DIGEST}

    def test_bare_path_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path, {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki"}}}
        )

        with pytest.raises(ValidationError, match="tag or a digest"):
            Config.load(tmp_path)

    def test_non_oci_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path, {"requires": ">=0.1", "k8s": {"charts": {"loki": "ghcr.io/grafana/helm-charts/loki:7.3.0"}}}
        )

        with pytest.raises(ValidationError, match="upstream-native OCI"):
            Config.load(tmp_path)

    def test_env_hole_rejected(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:${LOKI_VERSION}"}},
            },
        )

        with pytest.raises(ValidationError, match="tag or a digest"):
            Config.load(tmp_path)

    def test_missing_values_file_refuses_at_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}}},
        )
        write_lock(tmp_path)

        with pytest.raises(ValueError, match=r"values/loki\.yaml.*values file is required"):
            Config.load(tmp_path)


class TestK8sSection:
    def test_workloads_vars_and_charts_load(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "k8s": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
            },
        )

        config = Config.load(tmp_path)

        assert config is not None and config.k8s is not None
        assert config.k8s.workloads == ["api.yaml"]
        assert config.k8s.vars == {"GPU_VARIANT": "cuda"}

    def test_empty_section_rejected(self, tmp_path: Path) -> None:
        # strictyaml hands a bare section key an empty string, so an empty k8s section is
        # only spellable as the bare key — which refuses shape validation.
        (tmp_path / "docker-devkit.yaml").write_text('requires: ">=0.1"\nk8s:\n', encoding="utf-8")

        with pytest.raises(ValidationError, match="Input should be a valid dictionary"):
            Config.load(tmp_path)

    def test_vars_only_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "k8s": {"vars": {"GPU_VARIANT": "cuda"}}})

        with pytest.raises(ValidationError, match="must declare workloads or charts"):
            Config.load(tmp_path)


class TestServices:
    def test_groups_options_and_bare_entries_load(self, tmp_path: Path) -> None:
        write_edge_lock(tmp_path)
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "gateway": "${GATEWAY_PORT:?err}",
                "services": {
                    "auth": {
                        "options": {
                            "keycloak": {
                                "ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST,
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
                        "ref": "oci://ghcr.io/org/repo/livekit@" + DIGEST,
                        "routes": [{"path": "/livekit-token/*", "upstream": "livekit-token:8000"}],
                        "ports": [
                            {
                                "service": "livekit",
                                "published": "${LIVEKIT_SIGNALING_PORT:?err}",
                                "target": "${LIVEKIT_SIGNALING_PORT:?err}",
                                "protocol": "tcp",
                            }
                        ],
                    },
                },
            },
        )

        config = Config.load(tmp_path)

        assert config is not None
        bundles = config.compose_bundles()
        assert [bundle.name for bundle in bundles] == ["cuda", "rocm"]
        assert bundles[0].output == tmp_path / "stack/generated/compose/compose.cuda.yaml"
        assert bundles[0].vars == {"GPU_VARIANT": "cuda"}
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
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "workloads": ["api.yaml"],
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="ref: or workloads:, not both"):
            Config.load(tmp_path)

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
            Config.load(tmp_path)

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
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Config.load(tmp_path)

    def test_vars_beside_a_ref_refuse(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {
                    "remote": {
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "vars": {"GPU_VARIANT": "cuda"},
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="vars is not allowed with ref:"):
            Config.load(tmp_path)

    def test_an_unpinned_ref_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"remote": {"ref": "oci://ghcr.io/org/repo/x:main"}},
            },
        )

        with pytest.raises(ValidationError, match="digest-pinned OCI include"):
            Config.load(tmp_path)

    def test_an_empty_workloads_list_refuses(self) -> None:
        with pytest.raises(ValidationError, match="ref: or workloads: is required"):
            Config.model_validate({
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "services": {"empty": {"workloads": []}},
            })

    def test_services_require_project_name(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "gateway": "8080",
                "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST}},
            },
        )

        with pytest.raises(ValidationError, match="project_name is required"):
            Config.load(tmp_path)

    def test_local_entries_require_repository(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8080",
                "services": {"core": {"workloads": ["api.yaml"]}},
            },
        )

        with pytest.raises(ValidationError, match="repository is required"):
            Config.load(tmp_path)

    def test_remote_only_services_need_no_repository(self, tmp_path: Path) -> None:
        write_edge_lock(tmp_path)
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8080",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/livekit@" + DIGEST,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        assert Config.load(tmp_path) is not None


class TestFlatVocabulary:
    def _config(self, services: dict[str, object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "project_name": "example",
            "gateway": "8080",
            "services": services,
        }

    def test_an_option_colliding_with_an_entry_name_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._config({
                "livekit": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST},
                "tunnel": {"options": {"livekit": {"ref": "oci://ghcr.io/org/repo/y@" + DIGEST}}},
            }),
        )

        with pytest.raises(ValidationError, match=r"livekit is already an entry or option name"):
            Config.load(tmp_path)

    def test_an_option_colliding_across_groups_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._config({
                "one": {"options": {"cuda": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST}}},
                "two": {"options": {"cuda": {"ref": "oci://ghcr.io/org/repo/y@" + DIGEST}}},
            }),
        )

        with pytest.raises(ValidationError, match=r"cuda is already an entry or option name"):
            Config.load(tmp_path)


class TestAuthGroup:
    def _auth_config(self, options: dict[str, object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "project_name": "example",
            "gateway": "8080",
            "services": {"auth": {"options": options}},
        }

    def test_an_auth_option_with_exactly_one_route_loads(self, tmp_path: Path) -> None:
        write_edge_lock(tmp_path)
        write_config(
            tmp_path,
            self._auth_config({
                "keycloak": {
                    "ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST,
                    "routes": [{"path": "/auth/*", "upstream": "keycloak:8080", "strip_prefix": True}],
                }
            }),
        )

        assert Config.load(tmp_path) is not None

    def test_an_auth_option_without_a_route_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._auth_config({"keycloak": {"ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST}}),
        )

        with pytest.raises(ValidationError, match="auth option declares exactly one route"):
            Config.load(tmp_path)

    def test_an_auth_option_with_two_routes_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._auth_config({
                "keycloak": {
                    "ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST,
                    "routes": [
                        {"path": "/auth/*", "upstream": "keycloak:8080"},
                        {"path": "/auth2/*", "upstream": "keycloak:8080"},
                    ],
                }
            }),
        )

        with pytest.raises(ValidationError, match="exactly one route"):
            Config.load(tmp_path)

    def test_a_bare_entry_named_auth_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "services": {"auth": {"ref": "oci://ghcr.io/org/repo/keycloak@" + DIGEST}},
            },
        )

        with pytest.raises(ValidationError, match="auth is a reserved group name"):
            Config.load(tmp_path)


class TestRoutes:
    def _routed(self, routes: list[object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "project_name": "example",
            "gateway": "8080",
            "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST, "routes": routes}},
        }

    def test_edge_wide_path_uniqueness_refuses_duplicates(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8080",
                "services": {
                    "one": {
                        "options": {
                            "a": {
                                "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                                "routes": [{"path": "/x/*", "upstream": "x:1"}],
                            }
                        },
                    },
                    "two": {
                        "options": {
                            "b": {
                                "ref": "oci://ghcr.io/org/repo/y@" + DIGEST,
                                "routes": [{"path": "/x/*", "upstream": "y:1"}],
                            }
                        },
                    },
                },
            },
        )

        with pytest.raises(ValidationError, match="edge-wide unique"):
            Config.load(tmp_path)

    @pytest.mark.parametrize(
        "path",
        ["grafana/*", "//grafana/*", "/grafana//x", "/*/*", "/grafana /x", "grafana", "/grafana*", "/*grafana"],
    )
    def test_malformed_paths_refuse(self, path: str, tmp_path: Path) -> None:
        write_config(tmp_path, self._routed([{"path": path, "upstream": "api:8000"}]))

        with pytest.raises(ValidationError, match="must be a /-rooted segment chain"):
            Config.load(tmp_path)

    def test_plain_and_wildcard_paths_load(self, tmp_path: Path) -> None:
        write_edge_lock(tmp_path)
        write_config(
            tmp_path,
            self._routed([
                {"path": "/*", "upstream": "api:8000"},
                {"path": "/livekit-token/*", "upstream": "livekit-token:8000"},
            ]),
        )

        config = Config.load(tmp_path)

        assert config is not None

    def test_an_upstream_without_a_literal_port_refuses(self, tmp_path: Path) -> None:
        write_config(tmp_path, self._routed([{"path": "/*", "upstream": "api:${PORT}"}]))

        with pytest.raises(ValidationError, match="a service name and a literal port"):
            Config.load(tmp_path)

    def test_strip_prefix_on_a_non_wildcard_path_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._routed([{"path": "/grafana", "upstream": "grafana:3000", "strip_prefix": True}]),
        )

        with pytest.raises(ValidationError, match="must end in /\\*"):
            Config.load(tmp_path)

    def test_a_raw_route_loads(self, tmp_path: Path) -> None:
        write_edge_lock(tmp_path)
        write_config(
            tmp_path,
            self._routed([{"path": "/x/*", "upstream": "x:1", "raw": 'respond "hi" 200'}]),
        )

        config = Config.load(tmp_path)

        assert config is not None


class TestGatewayKey:
    def test_a_gateway_without_the_caddy_lock_entry_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8443",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        with pytest.raises(ValueError, match="CADDY_IMAGE"):
            Config.load(tmp_path)

    def test_a_routeless_stack_carries_an_inert_gateway(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8080",
                "services": {"livekit": {"ref": "oci://ghcr.io/org/repo/x@" + DIGEST}},
            },
        )

        config = Config.load(tmp_path)

        assert config is not None

    def test_a_literal_port_loads(self, tmp_path: Path) -> None:
        write_edge_lock(tmp_path)
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "8443",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        assert Config.load(tmp_path) is not None

    def test_a_malformed_gateway_value_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "project_name": "example",
                "gateway": "${gateway port}",
                "services": {
                    "livekit": {
                        "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    }
                },
            },
        )

        with pytest.raises(ValidationError, match="literal port number or a"):
            Config.load(tmp_path)


class TestPorts:
    def _ports_config(self, services: dict[str, object]) -> dict[str, object]:
        return {
            "requires": ">=0.1",
            "repository": "ghcr.io/org/repo",
            "project_name": "example",
            "gateway": "8080",
            "services": services,
        }

    def test_the_mapping_form_defaults_protocol_to_tcp(self, tmp_path: Path) -> None:
        write_edge_lock(tmp_path)
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": [
                        {"service": "livekit", "published": "${PORT:?err}", "target": "6178"},
                        {"service": "livekit", "published": "6179", "target": "6179", "protocol": "udp"},
                    ],
                }
            }),
        )

        config = Config.load(tmp_path)

        assert config is not None
        livekit = config.services["livekit"]
        assert isinstance(livekit, ServiceEntry)
        assert [(port.service, port.published, port.target, port.protocol) for port in livekit.ports] == [
            ("livekit", "${PORT:?err}", "6178", "tcp"),
            ("livekit", "6179", "6179", "udp"),
        ]

    def test_a_string_port_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": ["6179/6179/udp"],
                }
            }),
        )
        write_lock(tmp_path)

        with pytest.raises(ValidationError, match="Input should be a valid dictionary or instance of PortDeclaration"):
            Config.load(tmp_path)

    def test_an_empty_group_options_map_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "tunnel": {"options": None, "ports": [{"service": "ngrok", "published": "4040", "target": "4040"}]},
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                },
            }),
        )
        write_lock(tmp_path)

        with pytest.raises(ValidationError):
            Config.load(tmp_path)

    def test_the_mapping_form_spells_the_owning_service_and_host_ip(self, tmp_path: Path) -> None:
        write_edge_lock(tmp_path)
        write_config(
            tmp_path,
            self._ports_config({
                "placeframe": {
                    "options": {"cuda": {"workloads": ["api.yaml"]}},
                    "ports": [
                        {
                            "service": "api",
                            "host_ip": "127.0.0.1",
                            "published": "${API_DEBUG_PORT:-5679}",
                            "target": "5678",
                        }
                    ],
                },
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                },
            }),
        )

        config = Config.load(tmp_path)

        assert config is not None

    def test_a_non_port_non_hole_field_refuses(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            self._ports_config({
                "livekit": {
                    "ref": "oci://ghcr.io/org/repo/x@" + DIGEST,
                    "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                    "ports": [{"service": "livekit", "published": "eight", "target": "5678"}],
                }
            }),
        )

        with pytest.raises(ValidationError, match="literal port number or a"):
            Config.load(tmp_path)


class TestRepository:
    def test_repository_without_host_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "repository": "lbe-toolkit"})

        with pytest.raises(ValidationError, match="registry-host/repository-path"):
            Config.load(tmp_path)

    def test_trailing_slash_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "repository": "ghcr.io/org/repo/"})

        with pytest.raises(ValidationError, match="trailing slash"):
            Config.load(tmp_path)

    def test_repository_without_service_builds_loads(self, tmp_path: Path) -> None:
        # The publish shape for a repo whose whole runtime is its own workload set: no
        # Dockerfile service builds, repository naming the artifact registry.
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "repository": "ghcr.io/org/repo",
                "project_name": "example",
                "gateway": "8080",
                "services": {"rathole-client": {"workloads": ["rathole-client.yaml"]}},
            },
        )
        write_lock(tmp_path)

        config = Config.load(tmp_path)

        assert config is not None and config.repository == "ghcr.io/org/repo"
        assert config.service_builds == {}


class TestPlatforms:
    def test_arch_pairs_load(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "platforms": ["linux/arm64"]})

        config = Config.load(tmp_path)

        assert config is not None and config.platforms == ["linux/arm64"]

    def test_garbage_rejected(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "platforms": ["arm64"]})

        with pytest.raises(ValidationError, match="os/arch pair"):
            Config.load(tmp_path)

    def test_base_images_without_platforms_refuse_naming_the_fix(self, tmp_path: Path) -> None:
        write_config(tmp_path, {"requires": ">=0.1", "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"}})

        with pytest.raises(ValidationError, match=r"platforms is required.*\[linux/amd64\]"):
            Config.load(tmp_path)

    def test_charts_stay_exempt_from_the_platforms_rule(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}}},
        )
        write_lock(tmp_path)
        (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("loki: {}\n", encoding="utf-8")

        assert Config.load(tmp_path) is not None


class TestLockPairing:
    def test_base_images_without_lock_refuse(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "platforms": ["linux/amd64"],
                "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            },
        )

        with pytest.raises(ValueError, match="the committed lock is required"):
            Config.load(tmp_path)

    def test_charts_without_lock_refuse(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {"requires": ">=0.1", "k8s": {"charts": {"loki": "oci://ghcr.io/grafana/helm-charts/loki:7.3.0"}}},
        )
        (tmp_path / "stack" / "score" / "k8s" / "values").mkdir(parents=True)
        (tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml").write_text("loki: {}\n", encoding="utf-8")

        with pytest.raises(ValueError, match="the committed lock is required"):
            Config.load(tmp_path)

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

        assert Config.load(tmp_path) is not None

    def test_no_declarations_need_no_lock(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            {
                "requires": ">=0.1",
                "gateway": "8080",
                "services": {"livekit": {"ref": "oci://ghcr.io/x/y@" + DIGEST}},
                "project_name": "example",
            },
        )

        assert Config.load(tmp_path) is not None


class TestServiceDerivation:
    def test_repository_required_when_services_exist(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\n")
        write_config(tmp_path, {"requires": ">=0.1"})

        with pytest.raises(ValueError, match="repository is required"):
            Config.load(tmp_path)

    def test_derived_services_attach_to_the_loaded_config(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\n")
        write_config(tmp_path, {"requires": ">=0.1", "repository": "ghcr.io/org/repo"})

        config = Config.load(tmp_path)

        assert config is not None
        assert sorted(config.service_builds) == ["api"]


def test_direct_model_construction_is_valid_for_tests(tmp_path: Path) -> None:
    config = Config.model_validate({
        "requires": ">=0.1",
        "repository": "ghcr.io/org/repo",
        "project_name": "example",
        "gateway": "8080",
        "services": {"core": {"workloads": ["api.yaml"]}},
    })
    config.root = tmp_path

    assert config.compose_bundles()[0].name == "core"


def _write_raw_lock(tmp_path: Path, text: str) -> Path:
    lock = tmp_path / LOCK_PATH
    lock.write_text(text, encoding="utf-8")
    return lock


class TestConfigLockParse:
    def test_the_empty_lock_stays_legal(self, tmp_path: Path) -> None:
        assert parse_config_lock(_write_raw_lock(tmp_path, "")).images == {}
        assert parse_config_lock(_write_raw_lock(tmp_path, "{}\n")).charts == {}

    def test_malformed_yaml_refuses_naming_the_line(self, tmp_path: Path) -> None:
        lock = _write_raw_lock(tmp_path, "images:\n  A_IMAGE: [unclosed\n")

        with pytest.raises(ValueError, match=rf"^{re.escape(lock.as_posix())}:\d+: "):
            parse_config_lock(lock)

    def test_duplicate_keys_refuse_naming_the_line(self, tmp_path: Path) -> None:
        lock = _write_raw_lock(
            tmp_path,
            f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\n  A_IMAGE:\n    index: ghcr.io/x/a:2@{DIGEST}\n",
        )

        with pytest.raises(ValueError, match=rf"{re.escape(lock.as_posix())}: line 4: duplicate key 'A_IMAGE'"):
            parse_config_lock(lock)

    def test_the_env_line_format_refuses_as_a_bare_scalar(self, tmp_path: Path) -> None:
        lock = _write_raw_lock(tmp_path, f"A_IMAGE=ghcr.io/x/a:1@{DIGEST}\n")

        with pytest.raises(TypeError, match="mapping of images:/charts:"):
            parse_config_lock(lock)

    def test_a_non_digest_pinned_index_refuses(self, tmp_path: Path) -> None:
        lock = _write_raw_lock(tmp_path, "images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1.2.3\n")

        with pytest.raises(ValueError, match="not digest-pinned"):
            parse_config_lock(lock)

    def test_a_non_digest_pinned_platform_entry_refuses(self, tmp_path: Path) -> None:
        lock = _write_raw_lock(
            tmp_path,
            f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\n"
            "    platforms:\n      linux/amd64: ghcr.io/x/a:1\n",
        )

        with pytest.raises(ValueError, match="not digest-pinned"):
            parse_config_lock(lock)

    def test_a_non_digest_pinned_chart_refuses(self, tmp_path: Path) -> None:
        lock = _write_raw_lock(tmp_path, "charts:\n  LOKI_CHART: ghcr.io/x/loki:7.3.0\n")

        with pytest.raises(ValueError, match="chart LOKI_CHART is not digest-pinned"):
            parse_config_lock(lock)

    def test_a_malformed_platform_key_refuses(self, tmp_path: Path) -> None:
        lock = _write_raw_lock(
            tmp_path,
            f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\n"
            f"    platforms:\n      amd64: ghcr.io/x/a:1@{DIGEST}\n",
        )

        with pytest.raises(ValueError, match="os/arch pair"):
            parse_config_lock(lock)

    def test_a_name_in_both_sections_refuses(self, tmp_path: Path) -> None:
        lock = _write_raw_lock(
            tmp_path,
            f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\ncharts:\n  A_IMAGE: ghcr.io/x/a:1@{DIGEST}\n",
        )

        with pytest.raises(ValueError, match=r"images or charts, not both"):
            parse_config_lock(lock)

    def test_require_answers_the_index_for_images_and_the_flat_entry_for_charts(self, tmp_path: Path) -> None:
        lock = parse_config_lock(
            _write_raw_lock(
                tmp_path,
                f"images:\n  A_IMAGE:\n    index: ghcr.io/x/a:1@{DIGEST}\n"
                f"charts:\n  LOKI_CHART: ghcr.io/x/loki:7.3.0@{DIGEST}\n",
            )
        )

        assert lock.require("A_IMAGE", "subject") == f"ghcr.io/x/a:1@{DIGEST}"
        assert lock.require("LOKI_CHART", "subject") == f"ghcr.io/x/loki:7.3.0@{DIGEST}"

    def test_require_refuses_naming_the_lock_verb(self, tmp_path: Path) -> None:
        lock = parse_config_lock(_write_raw_lock(tmp_path, "{}\n"))

        with pytest.raises(ValueError, match=r"lacks the CADDY_IMAGE entry.*lock verb writes it"):
            lock.require("CADDY_IMAGE", "routes are declared")
