import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from boatyard import dockerfiles
from boatyard.config import Config
from boatyard.dockerfiles import ServiceBuild, compute_service_shas


def write_dockerfile(tmp_path: Path, service: str, text: str, top: str = "workloads") -> None:
    directory = tmp_path / top / service
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Dockerfile").write_text(text, encoding="utf-8")


def _derive(tmp_path: Path, axes: dict[str, list[str]] | None = None) -> dict[str, ServiceBuild]:
    payload: dict[str, Any] = {"requires": ">=0", "repository": "ghcr.io/example"}
    if axes is not None:
        payload["variants"] = axes
    (tmp_path / "docker-devkit.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    loaded = Config.load(tmp_path)
    assert loaded is not None
    return loaded.service_builds


def _service_builds(*dockerfiles: str) -> dict[str, ServiceBuild]:
    return {
        PurePosixPath(dockerfile).parent.name: ServiceBuild(
            name=PurePosixPath(dockerfile).parent.name, dockerfile=dockerfile
        )
        for dockerfile in dockerfiles
    }


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True, check=True)


def _commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init", "--allow-empty"], cwd=str(path), capture_output=True, check=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    (tmp_path / ".dockerignore").write_text("*\n!workloads/\n!packages/\n")
    (tmp_path / "workloads" / "app").mkdir(parents=True)
    (tmp_path / "workloads" / "app" / "main.py").write_text("print('hello')")
    (tmp_path / "workloads" / "worker").mkdir(parents=True)
    (tmp_path / "workloads" / "worker" / "run.py").write_text("print('work')")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "lib.py").write_text("x = 1")
    (tmp_path / "README.md").write_text("ignored")
    _commit_all(tmp_path)
    return tmp_path


class TestDeriveServices:
    def test_dockerfile_dirs_derive_bare_services(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "database-manager", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")

        services = _derive(tmp_path)

        # The service name IS the directory name — the old manifest's create-database key
        # renames to database-manager under directory-derived naming.
        assert sorted(services) == ["api", "database-manager"]
        assert [target for target, _variant in services["api"].target_variants] == ["api"]
        assert services["api"].dockerfile == "workloads/api/Dockerfile"
        assert services["api"].contexts == []

    def test_bare_identifier_from_declares_a_context_edge(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n")
        write_dockerfile(tmp_path, "localizer", "FROM neural-networks-base AS dev\nARG LOCALIZER_SHA\n")

        services = _derive(tmp_path)

        assert services["localizer"].contexts == ["neural-networks-base"]
        assert services["localizer"].sha_args == ["LOCALIZER_SHA"]

    def test_stage_names_are_not_context_edges(self, tmp_path: Path) -> None:
        write_dockerfile(
            tmp_path,
            "api",
            "ARG UV_IMAGE\nFROM ${UV_IMAGE} AS base\nFROM base AS dev\nFROM base AS prod\n",
        )

        services = _derive(tmp_path)

        assert services["api"].contexts == []

    def test_multi_stage_dockerfile_declares_a_dev_stage(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG PY_IMAGE\nFROM ${PY_IMAGE} AS base\nFROM base AS dev\n")

        services = _derive(tmp_path)

        assert services["api"].dev_stage

    def test_single_stage_dev_label_is_not_a_dev_stage(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG PY_IMAGE\nFROM ${PY_IMAGE} AS dev\n")

        services = _derive(tmp_path)

        assert not services["api"].dev_stage

    def test_bare_identifier_naming_no_sibling_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM python\n")

        with pytest.raises(ValueError, match="FROM python names no service directory"):
            _derive(tmp_path)

    def test_literal_from_refuses_naming_base_images(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "migrator", "FROM golang:1.22-alpine AS build\nFROM ghcr.io/org/base:1\n")

        with pytest.raises(ValueError, match=r"FROM golang:1\.22-alpine must reference a base_images build arg"):
            _derive(tmp_path)

    def test_registry_path_with_an_arg_suffix_still_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "FROM ghcr.io/org/uv${UV_BASE_DIGEST}\n")

        with pytest.raises(ValueError, match=r"FROM ghcr\.io/org/uv\$\{UV_BASE_DIGEST\} must reference"):
            _derive(tmp_path)

    def test_literal_copy_from_refuses_naming_base_images(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG PY_IMAGE\nFROM ${PY_IMAGE}\nCOPY --from=ghcr.io/org/uv:1 /uv /bin/\n")

        with pytest.raises(ValueError, match=r"COPY --from=ghcr\.io/org/uv:1 must reference a base_images build arg"):
            _derive(tmp_path)

    def test_stage_and_build_arg_copy_from_pass(self, tmp_path: Path) -> None:
        write_dockerfile(
            tmp_path,
            "api",
            "ARG PY_IMAGE\nFROM ${PY_IMAGE} AS base\nCOPY --from=base /x /y\nCOPY --from=${UV_IMAGE} /uv /bin/\n",
        )

        services = _derive(tmp_path)

        assert services["api"].contexts == []

    def test_a_declared_axis_arg_fans_over_the_declared_values(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG TORCH_DEVICE\n")

        services = _derive(tmp_path, {"TORCH_DEVICE": ["cuda", "rocm"]})

        assert services["neural-networks-base"].axis == "TORCH_DEVICE"
        assert services["neural-networks-base"].variants == ["cuda", "rocm"]
        assert [target for target, _variant in services["neural-networks-base"].target_variants] == [
            "neural-networks-base-cuda",
            "neural-networks-base-rocm",
        ]

    def test_an_arg_naming_no_declared_axis_stays_plain(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG TORCH_DEVICE\n")

        services = _derive(tmp_path)

        assert services["neural-networks-base"].axis is None
        assert services["neural-networks-base"].variants == []
        assert [target for target, _variant in services["neural-networks-base"].target_variants] == [
            "neural-networks-base"
        ]

    def test_declared_values_keep_their_declared_order(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG TORCH_DEVICE\n")

        services = _derive(tmp_path, {"TORCH_DEVICE": ["rocm", "cuda"]})

        assert services["api"].variants == ["rocm", "cuda"]
        assert [target for target, _variant in services["api"].target_variants] == ["api-rocm", "api-cuda"]

    def test_two_axis_args_on_one_service_refuse(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG ACCEL_FPGA\nARG ACCEL_TPU\n")

        with pytest.raises(ValueError, match="a service carries one axis"):
            _derive(tmp_path, {"ACCEL_FPGA": ["fpga"], "ACCEL_TPU": ["tpu"]})

    def test_the_axis_propagates_along_context_edges(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG TORCH_DEVICE\n")
        write_dockerfile(tmp_path, "localizer", "FROM neural-networks-base AS dev\n")

        services = _derive(tmp_path, {"TORCH_DEVICE": ["cuda", "rocm"]})

        assert services["localizer"].axis == "TORCH_DEVICE"
        assert services["localizer"].variants == ["cuda", "rocm"]
        assert [target for target, _variant in services["localizer"].target_variants] == [
            "localizer-cuda",
            "localizer-rocm",
        ]

    def test_an_independent_axis_over_a_variant_bearing_base_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "neural-networks-base", "ARG UV_IMAGE\nFROM ${UV_IMAGE}\nARG TORCH_DEVICE\n")
        write_dockerfile(tmp_path, "localizer", "FROM neural-networks-base AS dev\nARG TORCH_DEVICE\n")

        with pytest.raises(ValueError, match="variant-bearing base"):
            _derive(tmp_path, {"TORCH_DEVICE": ["cuda", "rocm"]})

    def test_parents_disagreeing_on_variant_cells_refuse(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "fanned-base", "ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\nARG TORCH_DEVICE\n")
        write_dockerfile(tmp_path, "plain-base", "ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\n")
        write_dockerfile(tmp_path, "dependent", "FROM fanned-base AS a\nFROM plain-base AS b\n")

        with pytest.raises(ValueError, match="disagree on variant cells"):
            _derive(tmp_path, {"TORCH_DEVICE": ["cuda", "rocm"]})

    def test_sha_arg_naming_no_service_refuses(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\nARG GHOST_SHA\n")

        with pytest.raises(ValueError, match="ARG GHOST_SHA names no service directory"):
            _derive(tmp_path)

    def test_dockerfiles_spanning_multiple_top_level_dirs_refuse(self, tmp_path: Path) -> None:
        write_dockerfile(tmp_path, "api", "ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\n")
        write_dockerfile(tmp_path, "bridge", "ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\n", top="docker")

        with pytest.raises(ValueError, match="span multiple top-level directories"):
            _derive(tmp_path)


class TestComputeServiceShasZeroServices:
    def _fail_bash(self, command: str) -> str:
        raise AssertionError(f"zero services must not shell out, saw: {command}")

    def test_a_serviceless_repo_returns_empty_without_git_or_dockerignore_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(dockerfiles, "bash_output", self._fail_bash)
        monkeypatch.setattr(dockerfiles, "bash", self._fail_bash)

        assert compute_service_shas(tmp_path, _service_builds()) == {}


class TestComputeServiceShas:
    def test_should_return_deterministic_hashes(self, repo: Path):
        builds = _service_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas1 = compute_service_shas(repo, builds)
        shas2 = compute_service_shas(repo, builds)

        assert shas1 == shas2

    def test_should_have_tree_prefix(self, repo: Path):
        shas = compute_service_shas(repo, _service_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        for sha in shas.values():
            assert sha.startswith("tree-")

    def test_should_return_only_tagged_services(self, repo: Path):
        shas = compute_service_shas(repo, _service_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile"))

        assert set(shas.keys()) == {"APP_SHA", "WORKER_SHA"}

    def test_should_change_only_affected_service_when_service_file_changes(self, repo: Path):
        builds = _service_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = compute_service_shas(repo, builds)

        (repo / "workloads" / "app" / "main.py").write_text("print('changed')")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, builds)
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] == shas_after["WORKER_SHA"]

    def test_should_change_all_services_when_shared_file_changes(self, repo: Path):
        builds = _service_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = compute_service_shas(repo, builds)

        (repo / "packages" / "lib.py").write_text("x = 2")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, builds)
        assert shas_before["APP_SHA"] != shas_after["APP_SHA"]
        assert shas_before["WORKER_SHA"] != shas_after["WORKER_SHA"]

    def test_should_not_change_when_ignored_file_changes(self, repo: Path):
        builds = _service_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = compute_service_shas(repo, builds)

        (repo / "README.md").write_text("changed readme")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, builds)
        assert shas_before == shas_after

    def test_should_not_change_when_untagged_workload_file_changes(self, repo: Path):
        builds = _service_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = compute_service_shas(repo, builds)

        (repo / "workloads" / "loki").mkdir()
        (repo / "workloads" / "loki" / "config.yaml").write_text("unused by tagged services")
        _commit_all(repo)

        shas_after = compute_service_shas(repo, builds)
        assert shas_before == shas_after

    def test_should_not_change_from_uncommitted_edits(self, repo: Path):
        builds = _service_builds("workloads/app/Dockerfile", "workloads/worker/Dockerfile")
        shas_before = compute_service_shas(repo, builds)

        (repo / "workloads" / "app" / "main.py").write_text("print('uncommitted')")

        shas_after = compute_service_shas(repo, builds)
        assert shas_before == shas_after

    def test_should_raise_when_services_span_multiple_top_level_dirs(self, repo: Path):
        builds = _service_builds("workloads/app/Dockerfile", "other/worker/Dockerfile")

        with pytest.raises(ValueError, match="multiple top-level"):
            compute_service_shas(repo, builds)
