import hashlib
import json
import os
import platform
import re
import shutil
from pathlib import Path
from subprocess import CalledProcessError
from typing import Any, get_args

import pytest
import yaml
from typer.testing import CliRunner

from docker_devkit import bake as bake_module
from docker_devkit.verbs import build
from docker_devkit.bake import DIGEST_FILE_NAME, render_bake
from docker_devkit.config import LOCK_PATH, DockerDevkitConfig
from docker_devkit.service_builds import ServiceBuild

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
BUILD_SHA = "c" * 40
MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
REPOSITORY = "ghcr.io/example"
REPO_ZED = "ghcr.io/example/zed-capture"
REPO_AOA = "ghcr.io/example/aoa-bridge"

runner = CliRunner()

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain_output(rendered: str) -> str:
    # Under GITHUB_ACTIONS rich renders refusals as styled panels: option spellings
    # carry escape codes between every dash-joined segment and lines wrap at panel
    # width, so assertions must read the message with styling and wrapping removed.
    unwrapped = re.sub(r"\s+", " ", _ANSI_ESCAPE.sub("", rendered))
    return unwrapped


def _write_repo(
    root: Path,
    *,
    dockerfiles: dict[str, str] | None = None,
    base_images: dict[str, str] | None = None,
    charts: dict[str, str] | None = None,
    platforms: list[str] | None = None,
    repository: str = REPOSITORY,
) -> None:
    for service, text in (dockerfiles or {}).items():
        directory = root / "workloads" / service
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Dockerfile").write_text(text, encoding="utf-8")
    payload: dict[str, Any] = {"requires": ">=0", "repository": repository}
    if base_images:
        payload["base_images"] = base_images
    if charts:
        payload["k8s"] = {"charts": charts}
        for chart in charts:
            values = root / "stack" / "score" / "k8s" / "values" / f"{chart}.yaml"
            values.parent.mkdir(parents=True, exist_ok=True)
            values.write_text("chart: {}\n", encoding="utf-8")
    if platforms:
        payload["platforms"] = platforms
    (root / "docker-devkit.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    if (base_images or charts) and not (root / LOCK_PATH).exists():
        # Declarations => the committed lock is required at load.
        (root / LOCK_PATH).write_text("", encoding="utf-8")


EXTERNAL = "FROM docker.io/library/python:3.13-slim\n"
FANNED = "ARG TORCH_DEVICE\nFROM docker.io/library/python:3.13-slim\n"
DEPENDENT = "FROM neural-networks-base\n"
DEV_STAGE = "FROM docker.io/library/python:3.13-slim AS base\nFROM base AS dev\n"
DEV_DEPENDENT = "FROM neural-networks-base AS base\nFROM base AS dev\n"
LYING_DEV_STAGE = "FROM docker.io/library/python:3.13-slim AS dev\n"

DEFAULT_DOCKERFILES: dict[str, str] = {
    "api": EXTERNAL,
    "postgres": EXTERNAL,
    "seaweedfs": EXTERNAL,
    "neural-networks-base": FANNED,
    "localizer": DEPENDENT,
    "reconstructor": DEPENDENT,
}


def _shard_key(targets: list[str]) -> str:
    return hashlib.sha256(",".join(sorted(targets)).encode("utf-8")).hexdigest()[:8]


def _bake_targets(commands: list[str]) -> list[str]:
    command = next(command for command in commands if command.startswith("docker buildx bake"))
    words = command.split()
    stance = "--load" if "--load" in words else "--push"
    return words[words.index(stance) + 1 :]


def _baked(target: str, *, refs: str, digest: str) -> dict[str, dict[str, str]]:
    return {target: {"image.name": refs, "containerimage.digest": digest}}


def _tagged_entry() -> dict[str, str]:
    return {"image.name": "registry/api:tree-abc", "containerimage.digest": DIGEST_A}


def _no_service_shas(root: Path, config: object) -> dict[str, str]:
    return {}


def _no_gc_limits() -> None:
    return None


def _amd64_host() -> str:
    return "linux/amd64"


def _fixed_digest(reference: str) -> str:
    return DIGEST_A


def _head_sha(command: str) -> str:
    return BUILD_SHA


def _noop_install_oras() -> None:
    return None


class DigestRecorder:
    def __init__(self) -> None:
        self.inspected: list[str] = []

    def resolve(self, reference: str) -> str:
        self.inspected.append(reference)
        return DIGEST_A


class BakeRunner:
    def __init__(self, entries: dict[str, dict[str, str]] | None = None) -> None:
        self.entries = entries or {}
        self.commands: list[str] = []
        self.rendered = ""

    def run(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("docker buildx bake"):
            words = command.split()
            bake_file = Path(words[words.index("-f") + 1])
            self.rendered = bake_file.read_text(encoding="utf-8")
            targets = _bake_targets([command])
            entries = {target: self.entries.get(target, _tagged_entry()) for target in targets}
            Path("metadata.json").write_text(json.dumps(entries), encoding="utf-8")


class BakeEnvProbe:
    def __init__(self) -> None:
        self.uv_image: str | None = None

    def run(self, command: str) -> None:
        self.uv_image = os.environ.get("UV_IMAGE")
        Path("metadata.json").write_text(json.dumps({"api": _tagged_entry()}), encoding="utf-8")


class PushBuildRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, Path, list[str]]] = []
        self.manifests: list[dict[str, Any]] = []

    def __call__(
        self,
        registry: str,
        project: str,
        platform: str,
        tag: str,
        source_directory: Path,
        paths: list[str],
    ) -> None:
        self.calls.append((registry, project, platform, tag, source_directory, paths))
        manifest_path = Path(source_directory) / paths[0]
        self.manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))


class TestDefaultTargets:
    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        arguments: list[str],
        dockerfiles: dict[str, str] | None = None,
        base_images: dict[str, str] | None = None,
        platforms: list[str] | None = None,
    ) -> list[str]:
        recorder = BakeRunner()
        monkeypatch.setattr(bake_module, "bash", recorder.run)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "host_platform", _amd64_host)
        monkeypatch.chdir(tmp_path)
        _write_repo(
            tmp_path,
            dockerfiles=dockerfiles or DEFAULT_DOCKERFILES,
            base_images=base_images,
            platforms=platforms,
        )

        result = runner.invoke(build.app, arguments)

        assert result.exit_code == 0, result.output
        return _bake_targets(recorder.commands)

    def test_none_yields_common_partition(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--gpu", "none"]) == ["api", "postgres", "seaweedfs"]

    def test_gpu_only_yields_just_that_gpus_targets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--gpu", "cuda", "--gpu-only"]) == [
            "localizer-cuda",
            "neural-networks-base-cuda",
            "reconstructor-cuda",
        ]
        assert self._invoke(monkeypatch, tmp_path, ["--gpu", "rocm", "--gpu-only"]) == [
            "localizer-rocm",
            "neural-networks-base-rocm",
            "reconstructor-rocm",
        ]

    def test_default_with_gpu_yields_common_plus_that_gpu(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--gpu", "cuda"]) == [
            "api",
            "localizer-cuda",
            "neural-networks-base-cuda",
            "postgres",
            "reconstructor-cuda",
            "seaweedfs",
        ]

    def test_foreign_arch_repo_refuses_bare_build(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(bake_module, "bash", BakeRunner().run)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "host_platform", _amd64_host)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"zed-capture": EXTERNAL}, platforms=["linux/arm64"])

        result = runner.invoke(build.app, ["--gpu", "none"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "this machine's arch" in str(result.exception)
        assert "--targets" in str(result.exception)

    def test_foreign_arch_repo_builds_under_explicit_targets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(
            monkeypatch,
            tmp_path,
            ["--gpu", "none", "--targets", "zed-capture"],
            dockerfiles={"zed-capture": EXTERNAL},
            platforms=["linux/arm64"],
        ) == ["zed-capture"]

    def test_multiarch_repo_pins_the_host_platform_in_load_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        bake_runner = BakeRunner()
        monkeypatch.setattr(bake_module, "bash", bake_runner.run)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "host_platform", _amd64_host)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": EXTERNAL}, platforms=["linux/amd64", "linux/arm64"])

        result = runner.invoke(build.app, ["--gpu", "none"])

        assert result.exit_code == 0, result.output
        assert _bake_targets(bake_runner.commands) == ["api"]
        assert yaml.safe_load(bake_runner.rendered)["services"]["api"]["build"]["platforms"] == ["linux/amd64"]

    def test_bake_invokes_the_rendered_temp_file_without_set_strings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        bake_runner = BakeRunner()
        monkeypatch.setattr(bake_module, "bash", bake_runner.run)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "host_platform", _amd64_host)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": EXTERNAL})

        result = runner.invoke(build.app, ["--gpu", "none"])

        assert result.exit_code == 0, result.output
        command = bake_runner.commands[0]
        assert command.startswith("docker buildx bake -f ")
        assert "--set" not in command
        assert "metadata.json" in command

    def test_unknown_targets_refuse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(bake_module, "bash", BakeRunner().run)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "host_platform", _amd64_host)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": EXTERNAL})

        result = runner.invoke(build.app, ["--gpu", "none", "--targets", "missing"])

        assert result.exit_code != 0
        assert "Unknown targets" in result.output

    def test_missing_config_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(build.app, ["--gpu", "none"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "No docker-devkit.yaml" in str(result.exception)

    def test_explicit_targets_never_probe_the_host_gpu(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = BakeRunner()
        monkeypatch.setattr(bake_module, "bash", recorder.run)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "host_platform", _amd64_host)

        def _refuse_probe() -> str:
            raise AssertionError("explicit --targets must not probe the host gpu")

        monkeypatch.setattr(bake_module, "detect_gpu", _refuse_probe)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": EXTERNAL})

        result = runner.invoke(build.app, ["--targets", "api"])

        assert result.exit_code == 0, result.output
        assert _bake_targets(recorder.commands) == ["api"]

    def test_default_targets_resolve_auto_through_detection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = BakeRunner()
        monkeypatch.setattr(bake_module, "bash", recorder.run)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "host_platform", _amd64_host)
        monkeypatch.setattr(bake_module, "detect_gpu", lambda: "cuda")
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles=DEFAULT_DOCKERFILES)

        result = runner.invoke(build.app, [])

        assert result.exit_code == 0, result.output
        assert _bake_targets(recorder.commands) == [
            "api",
            "localizer-cuda",
            "neural-networks-base-cuda",
            "postgres",
            "reconstructor-cuda",
            "seaweedfs",
        ]


class TestBuildKeysGuard:
    def _prepare(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(
            tmp_path,
            dockerfiles=None,
            base_images={"RATHOLE_IMAGE": "docker.io/outernet-foundation/rathole:0.11.0"},
            repository="ghcr.io/example/infra-rathole",
        )

    def test_build_without_derived_services_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._prepare(monkeypatch, tmp_path)

        result = runner.invoke(build.app, ["--gpu", "none"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "no Dockerfile service derives" in str(result.exception)

    def test_lock_only_without_services_writes_the_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = DigestRecorder()
        monkeypatch.setattr(bake_module, "resolve_remote_digest", recorder.resolve)
        self._prepare(monkeypatch, tmp_path)

        result = runner.invoke(build.app, ["--lock-only"])

        assert result.exit_code == 0, result.output
        lock = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        assert lock.startswith("RATHOLE_IMAGE=")

    def test_relock_prunes_entries_whose_declarations_died(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = DigestRecorder()
        monkeypatch.setattr(bake_module, "resolve_remote_digest", recorder.resolve)
        self._prepare(monkeypatch, tmp_path)
        (tmp_path / LOCK_PATH).write_text(
            "DEAD_OPERATOR_IMAGE=ghcr.io/outernet-foundation/mirror/quay.io/op/operator:1.0@sha256:" + "b" * 64 + "\n"
            "RATHOLE_IMAGE=ghcr.io/outernet-foundation/mirror/docker.io/outernet-foundation/rathole:0.11.0@sha256:"
            + "c" * 64
            + "\n",
            encoding="utf-8",
        )

        result = runner.invoke(build.app, ["--lock-only"])

        assert result.exit_code == 0, result.output
        lock = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        assert lock == (
            "RATHOLE_IMAGE=ghcr.io/outernet-foundation/mirror/docker.io/outernet-foundation/rathole:0.11.0@sha256:"
            + "c" * 64
            + "\n"
        )


class TestLockOnlySelfSkip:
    def test_lock_only_skips_without_a_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(build.app, ["--lock-only"])

        assert result.exit_code == 0, result.output
        assert "no lock declarations to relock" in result.output
        assert not (tmp_path / "workloads").exists()

    def test_lock_only_skips_without_declarations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": EXTERNAL})

        result = runner.invoke(build.app, ["--lock-only"])

        assert result.exit_code == 0, result.output
        assert "nothing to relock" in result.output
        assert not (tmp_path / LOCK_PATH).exists()

    def test_lock_only_skips_in_a_compose_only_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles=None)

        result = runner.invoke(build.app, ["--lock-only"])

        assert result.exit_code == 0, result.output
        assert not (tmp_path / LOCK_PATH).exists()

    def test_building_run_without_config_still_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(build.app, ["--gpu", "none"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "No docker-devkit.yaml" in str(result.exception)


class TestDevBuilds:
    def _invoke_dev(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        arguments: list[str],
        dockerfiles: dict[str, str] | None = None,
    ) -> tuple[list[str], str]:
        recorder = BakeRunner()
        monkeypatch.setattr(bake_module, "bash", recorder.run)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "host_platform", _amd64_host)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles=dockerfiles or {"api": DEV_STAGE, "postgres": EXTERNAL})

        result = runner.invoke(build.app, arguments)

        assert result.exit_code == 0, result.output
        return _bake_targets(recorder.commands), recorder.rendered

    def test_dev_builds_only_dev_stage_services(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        targets, _rendered = self._invoke_dev(monkeypatch, tmp_path, ["--dev", "--gpu", "none"])

        assert targets == ["api"]

    def test_dev_builds_the_host_gpu_variant_of_fanned_services(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        targets, _rendered = self._invoke_dev(
            monkeypatch,
            tmp_path,
            ["--dev", "--gpu", "cuda"],
            dockerfiles={"neural-networks-base": FANNED, "localizer": DEV_DEPENDENT},
        )

        # The fan base declares no dev stage, so only localizer's host-variant dev
        # target builds.
        assert targets == ["localizer-cuda"]

    def test_dev_renders_target_dev_under_the_service_dev_tag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _targets, rendered = self._invoke_dev(monkeypatch, tmp_path, ["--dev", "--gpu", "none"])
        build_block = yaml.safe_load(rendered)["services"]["api"]["build"]

        assert build_block["target"] == "dev"
        assert build_block["tags"] == [f"{REPOSITORY}/api:dev"]
        assert "cache_to" not in build_block

    def test_fanned_dev_targets_share_one_service_dev_tag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _targets, rendered = self._invoke_dev(
            monkeypatch,
            tmp_path,
            ["--dev", "--gpu", "rocm"],
            dockerfiles={"neural-networks-base": FANNED, "localizer": DEV_DEPENDENT},
        )
        services = yaml.safe_load(rendered)["services"]

        assert services["localizer-cuda"]["build"]["tags"] == [f"{REPOSITORY}/localizer:dev"]
        assert services["localizer-rocm"]["build"]["tags"] == [f"{REPOSITORY}/localizer:dev"]

    def test_dev_refuses_push(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": DEV_STAGE})

        result = runner.invoke(build.app, ["--dev", "--push", "--gpu", "cuda"])

        assert result.exit_code != 0
        assert "local-only" in result.output

    def test_dev_refuses_lock_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": DEV_STAGE})

        result = runner.invoke(build.app, ["--dev", "--lock-only"])

        assert result.exit_code != 0
        assert "--lock-only" in _plain_output(result.output)

    def test_dev_without_any_dev_stage_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "detect_gpu", lambda: "none")
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": EXTERNAL, "liar": LYING_DEV_STAGE})

        result = runner.invoke(build.app, ["--dev"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "no service declares a dev stage" in str(result.exception)

    def test_dev_explicit_target_without_dev_stage_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": EXTERNAL, "auth": DEV_STAGE})

        result = runner.invoke(build.app, ["--dev", "--targets", "api"])

        assert result.exit_code != 0
        assert "no dev stage" in result.output


class TestBakeFailure:
    def test_failed_bake_raises_with_the_rendered_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        def _fail(command: str) -> None:
            raise CalledProcessError(returncode=1, cmd=command)

        monkeypatch.setattr(bake_module, "bash", _fail)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "host_platform", _amd64_host)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": EXTERNAL})

        result = runner.invoke(build.app, ["--gpu", "none"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "rendered bake file" in str(result.exception)
        assert f"{REPOSITORY}/api:latest" in str(result.exception)


class TestPushShelving:
    def _prepare(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entries: dict[str, dict[str, str]]
    ) -> PushBuildRecorder:
        recorder = PushBuildRecorder()
        monkeypatch.setattr(bake_module, "bash", BakeRunner(entries).run)
        monkeypatch.setattr(bake_module, "bash_output", _head_sha)
        monkeypatch.setattr(bake_module, "push_build", recorder)
        monkeypatch.setattr(bake_module, "install_oras", _noop_install_oras)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.setenv("REGISTRY", "ghcr.io/owner")
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"zed-capture": EXTERNAL, "aoa-bridge": EXTERNAL})
        return recorder

    def test_shelves_the_distilled_manifest_under_images_digests_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        entries = _baked("zed-capture", refs=f"{REPO_ZED}:tree-abc,{REPO_ZED}:latest", digest=DIGEST_A)
        recorder = self._prepare(monkeypatch, tmp_path, entries)

        result = runner.invoke(build.app, ["--push", "--gpu", "cuda", "--targets", "zed-capture"])

        assert result.exit_code == 0, result.output
        assert len(recorder.calls) == 1
        registry, project, platform, tag, _source_directory, paths = recorder.calls[0]
        assert registry == "ghcr.io/owner/builds"
        assert project == "images-digests"
        assert platform == "all"
        assert tag == f"sha-{BUILD_SHA}-{_shard_key(['zed-capture'])}"
        assert paths == [DIGEST_FILE_NAME]
        assert recorder.manifests == [
            {"zed-capture": {"ref": REPO_ZED, "digest": DIGEST_A, "tags": ["tree-abc", "latest"]}}
        ]

    def test_distills_multiple_targets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        entries = {
            **_baked("zed-capture", refs=f"{REPO_ZED}:tree-abc", digest=DIGEST_A),
            **_baked("aoa-bridge", refs=f"{REPO_AOA}:tree-def", digest=DIGEST_B),
        }
        recorder = self._prepare(monkeypatch, tmp_path, entries)

        result = runner.invoke(
            build.app, ["--push", "--gpu", "cuda", "--targets", "zed-capture", "--targets", "aoa-bridge"]
        )

        assert result.exit_code == 0, result.output
        assert recorder.manifests == [
            {
                "zed-capture": {"ref": REPO_ZED, "digest": DIGEST_A, "tags": ["tree-abc"]},
                "aoa-bridge": {"ref": REPO_AOA, "digest": DIGEST_B, "tags": ["tree-def"]},
            }
        ]

    def test_strips_digest_suffix_from_refs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        entries = _baked("zed-capture", refs=f"{REPO_ZED}:tree-abc@{DIGEST_A}", digest=DIGEST_A)
        recorder = self._prepare(monkeypatch, tmp_path, entries)

        result = runner.invoke(build.app, ["--push", "--gpu", "cuda", "--targets", "zed-capture"])

        assert result.exit_code == 0, result.output
        entry = recorder.manifests[0]["zed-capture"]
        assert entry["ref"] == REPO_ZED
        assert entry["tags"] == ["tree-abc"]

    def test_refuses_target_with_no_tagged_references(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        entries = _baked("zed-capture", refs=f"{REPO_ZED}@{DIGEST_A}", digest=DIGEST_A)
        self._prepare(monkeypatch, tmp_path, entries)

        result = runner.invoke(build.app, ["--push", "--gpu", "cuda"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)

    def test_shard_tag_is_order_insensitive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        entries = {
            **_baked("zed-capture", refs=f"{REPO_ZED}:tree-abc", digest=DIGEST_A),
            **_baked("aoa-bridge", refs=f"{REPO_AOA}:tree-def", digest=DIGEST_B),
        }
        first = self._prepare(monkeypatch, tmp_path, entries)

        result_first = runner.invoke(
            build.app, ["--push", "--gpu", "cuda", "--targets", "zed-capture aoa-bridge"]
        )

        assert result_first.exit_code == 0, result_first.output
        second = self._prepare(monkeypatch, tmp_path, entries)

        result_second = runner.invoke(
            build.app, ["--push", "--gpu", "cuda", "--targets", "aoa-bridge", "--targets", "zed-capture"]
        )

        assert result_second.exit_code == 0, result_second.output
        expected = f"sha-{BUILD_SHA}-{_shard_key(['zed-capture', 'aoa-bridge'])}"
        assert first.calls[0][3] == expected
        assert second.calls[0][3] == expected

    def test_push_mode_renders_registry_cache_refs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        bake_runner = BakeRunner(_baked("zed-capture", refs=f"{REPO_ZED}:tree-abc", digest=DIGEST_A))
        monkeypatch.setattr(bake_module, "bash", bake_runner.run)
        monkeypatch.setattr(bake_module, "bash_output", _head_sha)
        monkeypatch.setattr(bake_module, "push_build", PushBuildRecorder())
        monkeypatch.setattr(bake_module, "install_oras", _noop_install_oras)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(bake_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.setenv("REGISTRY", "ghcr.io/owner")
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"zed-capture": EXTERNAL})

        result = runner.invoke(build.app, ["--push", "--gpu", "cuda", "--targets", "zed-capture"])

        assert result.exit_code == 0, result.output
        build_block = yaml.safe_load(bake_runner.rendered)["services"]["zed-capture"]["build"]
        assert build_block["cache_to"] == [
            f"type=registry,ref={REPOSITORY}/build-cache:zed-capture,mode=max,image-manifest=true,oci-mediatypes=true"
        ]
        assert build_block["cache_from"] == [f"type=registry,ref={REPOSITORY}/build-cache:zed-capture"]


LOCK_DOCKERFILES: dict[str, str] = {"api": EXTERNAL}
LOCK_BASE_IMAGES = {
    "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
    "API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
}


class TestLockRecording:
    def _invoke_lock(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        arguments: list[str],
        base_images: dict[str, str] | None = None,
        charts: dict[str, str] | None = None,
    ) -> DigestRecorder:
        recorder = DigestRecorder()
        monkeypatch.setattr(bake_module, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(
            tmp_path,
            dockerfiles=LOCK_DOCKERFILES,
            base_images=base_images or dict(LOCK_BASE_IMAGES),
            charts=charts,
        )

        result = runner.invoke(build.app, arguments)

        assert result.exit_code == 0, result.output
        return recorder

    def test_records_declarations_under_the_mirror_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text(
            f"API_IMAGE={MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.2@{DIGEST_A}\n",
            encoding="utf-8",
        )

        recorder = self._invoke_lock(monkeypatch, tmp_path, ["--lock-only"])

        assert recorder.inspected == [
            "ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
        ]
        lock = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        assert f"API_IMAGE={MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{DIGEST_A}" in lock
        assert f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}" in lock

    def test_written_lock_carries_no_generated_header(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_lock(monkeypatch, tmp_path, ["--lock-only"])

        lock = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        assert lock.startswith("API_IMAGE=")

    def test_digest_pinned_declarations_pass_through_prefixed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = self._invoke_lock(
            monkeypatch, tmp_path, ["--lock-only"], base_images={"ALPINE_IMAGE": f"docker.io/library/alpine@{DIGEST_A}"}
        )

        assert recorder.inspected == []
        lock = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        assert f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine@{DIGEST_A}" in lock

    def test_cached_entries_resolve_again_only_on_upgrade(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / "workloads").mkdir()
        (tmp_path / LOCK_PATH).write_text(
            f"API_IMAGE={MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{DIGEST_A}\n"
            f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}\n",
            encoding="utf-8",
        )

        recorder = self._invoke_lock(monkeypatch, tmp_path, ["--lock-only"])
        assert recorder.inspected == []

        recorder = self._invoke_lock(monkeypatch, tmp_path, ["--lock-only", "--upgrade"])
        assert recorder.inspected == [
            "ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
        ]


LOKI_UPSTREAM = "ghcr.io/grafana/helm-charts/loki"


class TestChartLockRecording:
    def test_tag_declared_chart_resolves_under_its_chart_lock_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        recorder = DigestRecorder()
        monkeypatch.setattr(bake_module, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles=None, charts={"loki": f"oci://{LOKI_UPSTREAM}:7.3.0"})

        result = runner.invoke(build.app, ["--lock-only"])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == [f"{LOKI_UPSTREAM}:7.3.0"]
        lock = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        assert f"LOKI_CHART={MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_A}" in lock

    def test_digest_declared_chart_passes_through_unresolved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = DigestRecorder()
        monkeypatch.setattr(bake_module, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles=None, charts={"loki": f"oci://{LOKI_UPSTREAM}@{DIGEST_B}"})

        result = runner.invoke(build.app, ["--lock-only"])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == []
        lock = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        assert f"LOKI_CHART={MIRROR_PREFIX}/{LOKI_UPSTREAM}@{DIGEST_B}" in lock

    def test_unchanged_chart_declaration_copies_its_frozen_digest_forward(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        recorder = DigestRecorder()
        monkeypatch.setattr(bake_module, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles=None, charts={"loki": f"oci://{LOKI_UPSTREAM}:7.3.0"})
        (tmp_path / LOCK_PATH).write_text(
            f"LOKI_CHART={MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_B}\n", encoding="utf-8"
        )

        result = runner.invoke(build.app, ["--lock-only"])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == []
        lock = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        assert f"LOKI_CHART={MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_B}" in lock

    def test_chart_tag_bump_re_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = DigestRecorder()
        monkeypatch.setattr(bake_module, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles=None, charts={"loki": f"oci://{LOKI_UPSTREAM}:7.3.1"})
        (tmp_path / LOCK_PATH).write_text(
            f"LOKI_CHART={MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_B}\n", encoding="utf-8"
        )

        result = runner.invoke(build.app, ["--lock-only"])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == [f"{LOKI_UPSTREAM}:7.3.1"]
        lock = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        assert f"LOKI_CHART={MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.1@{DIGEST_A}" in lock


class TestBakeSeesLockValues:
    def test_bake_sees_mirror_prefixed_lock_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        probe = BakeEnvProbe()
        monkeypatch.setattr(bake_module, "bash", probe.run)
        monkeypatch.setattr(bake_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(bake_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles=LOCK_DOCKERFILES, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})

        result = runner.invoke(build.app, ["--gpu", "none"])

        assert result.exit_code == 0, result.output
        assert probe.uv_image == f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}"


class TestLockCheck:
    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        arguments: list[str],
        *,
        base_images: dict[str, str] | None = None,
    ) -> Any:
        recorder = DigestRecorder()
        monkeypatch.setattr(bake_module, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(bake_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(
            tmp_path,
            dockerfiles=LOCK_DOCKERFILES,
            base_images=LOCK_BASE_IMAGES if base_images is None else base_images,
        )
        return runner.invoke(build.app, arguments)

    def test_fresh_lock_passes_and_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--lock-only"]).exit_code == 0
        before = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")

        result = self._invoke(monkeypatch, tmp_path, ["--lock-only", "--check"])

        assert result.exit_code == 0, result.output
        assert (tmp_path / LOCK_PATH).read_text(encoding="utf-8") == before

    def test_stale_lock_exits_nonzero_and_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text(
            f"API_IMAGE={MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.2@{DIGEST_A}\n",
            encoding="utf-8",
        )

        result = self._invoke(monkeypatch, tmp_path, ["--lock-only", "--check"])

        assert result.exit_code == 1, result.output
        assert "image lock is stale" in result.output
        assert (tmp_path / LOCK_PATH).read_text(encoding="utf-8").startswith("API_IMAGE=")

    def test_self_skips_where_no_declarations_exist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = self._invoke(monkeypatch, tmp_path, ["--lock-only", "--check"], base_images={})

        assert result.exit_code == 0, result.output
        assert "nothing to relock" in result.output

    def test_check_without_lock_only_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = self._invoke(monkeypatch, tmp_path, ["--check"])

        assert result.exit_code != 0, result.output
        assert "pass --lock-only" in _plain_output(result.output)


def _render_config(
    services: dict[str, ServiceBuild],
    *,
    repository: str | None = REPOSITORY,
    base_images: dict[str, str] | None = None,
    platforms: list[str] | None = None,
) -> DockerDevkitConfig:
    config = DockerDevkitConfig.model_construct(
        requires=">=0",
        repository=repository,
        base_images=base_images or {},
        platforms=platforms or [],
    )
    config.service_builds = services
    return config


def _rendered_services(rendered: str) -> dict[str, Any]:
    return yaml.safe_load(rendered)["services"]


class TestRenderBakeDeterminism:
    def test_identical_configs_render_byte_identical_files(self):
        def build() -> DockerDevkitConfig:
            return _render_config(
                {
                    "api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile"),
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        contexts=["neural-networks-base"],
                        variants=["cuda", "rocm"],
                        sha_args=["LOCALIZER_SHA"],
                    ),
                },
                base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            )

        assert render_bake(build(), push=False, host="linux/amd64") == render_bake(
            build(), push=False, host="linux/amd64"
        )


class TestRenderBakeTagDerivation:
    def test_unfanned_service_carries_one_target_with_latest_and_sha_tags(self):
        services = _rendered_services(
            render_bake(
                _render_config({"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")}),
                push=False,
                host="linux/amd64",
            )
        )

        assert list(services) == ["api"]
        assert services["api"]["build"]["tags"] == [
            f"{REPOSITORY}/api:latest",
            f"{REPOSITORY}/api:${{API_SHA}}",
        ]

    def test_variant_fans_derive_per_variant_targets_sharing_one_sha_env(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        variants=["cuda", "rocm"],
                    )
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert list(services) == ["localizer-cuda", "localizer-rocm"]
        assert services["localizer-cuda"]["build"]["tags"] == [
            f"{REPOSITORY}/localizer-cuda:latest",
            f"{REPOSITORY}/localizer-cuda:${{LOCALIZER_SHA}}",
        ]
        assert services["localizer-rocm"]["build"]["tags"] == [
            f"{REPOSITORY}/localizer-rocm:latest",
            f"{REPOSITORY}/localizer-rocm:${{LOCALIZER_SHA}}",
        ]


class TestRenderBakeArgs:
    def test_every_base_image_key_is_injected_on_every_service(self):
        services = _rendered_services(
            render_bake(
                _render_config(
                    {"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")},
                    base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15", "ALPINE_IMAGE": "alpine:3.20"},
                ),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["api"]["build"]["args"] == {
            "UV_BASE_IMAGE": "${UV_BASE_IMAGE:-}",
            "ALPINE_IMAGE": "${ALPINE_IMAGE:-}",
        }

    def test_sha_args_render_only_for_declaring_dockerfiles(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile"),
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        sha_args=["LOCALIZER_SHA"],
                    ),
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert "LOCALIZER_SHA" not in services["api"]["build"]["args"]
        assert services["localizer"]["build"]["args"] == {"LOCALIZER_SHA": "${LOCALIZER_SHA:?err}"}

    def test_fanned_targets_carry_the_torch_device_literal(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        variants=["cuda", "rocm"],
                    )
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["localizer-cuda"]["build"]["args"]["TORCH_DEVICE"] == "cuda"
        assert services["localizer-rocm"]["build"]["args"]["TORCH_DEVICE"] == "rocm"


class TestRenderBakeContexts:
    def test_contexts_render_target_prefixed_and_variant_matched(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "neural-networks-base": ServiceBuild(
                        name="neural-networks-base",
                        dockerfile="workloads/neural-networks-base/Dockerfile",
                        variants=["cuda", "rocm"],
                    ),
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        contexts=["neural-networks-base"],
                        variants=["cuda", "rocm"],
                    ),
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["localizer-cuda"]["build"]["additional_contexts"] == {
            "neural-networks-base": "target:neural-networks-base-cuda"
        }
        assert services["localizer-rocm"]["build"]["additional_contexts"] == {
            "neural-networks-base": "target:neural-networks-base-rocm"
        }

    def test_unfanned_context_references_the_bare_target(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "base": ServiceBuild(name="base", dockerfile="workloads/base/Dockerfile"),
                    "app": ServiceBuild(name="app", dockerfile="workloads/app/Dockerfile", contexts=["base"]),
                }),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["app"]["build"]["additional_contexts"] == {"base": "target:base"}


class TestRenderBakeCache:
    def test_push_mode_renders_registry_cache_refs_per_target(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        variants=["cuda"],
                    )
                }),
                push=True,
                host="linux/amd64",
            )
        )

        assert services["localizer-cuda"]["build"]["cache_to"] == [
            (
                f"type=registry,ref={REPOSITORY}/build-cache:localizer-cuda,"
                "mode=max,image-manifest=true,oci-mediatypes=true"
            )
        ]
        assert services["localizer-cuda"]["build"]["cache_from"] == [
            f"type=registry,ref={REPOSITORY}/build-cache:localizer-cuda"
        ]

    def test_load_mode_renders_no_cache_refs(self):
        services = _rendered_services(
            render_bake(
                _render_config({"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")}),
                push=False,
                host="linux/amd64",
            )
        )

        assert "cache_to" not in services["api"]["build"]
        assert "cache_from" not in services["api"]["build"]


class TestRenderBakeDev:
    def test_dev_targets_the_dev_stage_under_one_service_dev_tag(self):
        services = _rendered_services(
            render_bake(
                _render_config({"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")}),
                push=False,
                host="linux/amd64",
                dev=True,
            )
        )

        assert services["api"]["build"]["target"] == "dev"
        assert services["api"]["build"]["tags"] == [f"{REPOSITORY}/api:dev"]

    def test_fanned_variants_share_the_service_dev_tag(self):
        services = _rendered_services(
            render_bake(
                _render_config({
                    "localizer": ServiceBuild(
                        name="localizer",
                        dockerfile="workloads/localizer/Dockerfile",
                        variants=["cuda", "rocm"],
                    )
                }),
                push=False,
                host="linux/amd64",
                dev=True,
            )
        )

        assert services["localizer-cuda"]["build"]["tags"] == [f"{REPOSITORY}/localizer:dev"]
        assert services["localizer-rocm"]["build"]["tags"] == [f"{REPOSITORY}/localizer:dev"]


class TestRenderBakePlatforms:
    def test_multi_arch_declaration_collapses_to_the_host_in_load_mode(self):
        services = _rendered_services(
            render_bake(
                _render_config(
                    {"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")},
                    platforms=["linux/amd64", "linux/arm64"],
                ),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["api"]["build"]["platforms"] == ["linux/amd64"]

    def test_multi_arch_declaration_rides_through_in_push_mode(self):
        services = _rendered_services(
            render_bake(
                _render_config(
                    {"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")},
                    platforms=["linux/amd64", "linux/arm64"],
                ),
                push=True,
                host="linux/amd64",
            )
        )

        assert services["api"]["build"]["platforms"] == ["linux/amd64", "linux/arm64"]

    def test_single_declared_platform_rides_through_in_load_mode(self):
        services = _rendered_services(
            render_bake(
                _render_config(
                    {"zed-capture": ServiceBuild(name="zed-capture", dockerfile="workloads/zed-capture/Dockerfile")},
                    platforms=["linux/arm64"],
                ),
                push=False,
                host="linux/amd64",
            )
        )

        assert services["zed-capture"]["build"]["platforms"] == ["linux/arm64"]

    def test_undeclared_platforms_render_no_key(self):
        services = _rendered_services(
            render_bake(
                _render_config({"api": ServiceBuild(name="api", dockerfile="workloads/api/Dockerfile")}),
                push=False,
                host="linux/amd64",
            )
        )

        assert "platforms" not in services["api"]["build"]


def test_render_bake_carries_only_bake_read_keys():
    rendered = render_bake(
        _render_config(
            {
                "api": ServiceBuild(
                    name="api",
                    dockerfile="workloads/api/Dockerfile",
                    contexts=["base"],
                    sha_args=["API_SHA"],
                ),
                "base": ServiceBuild(name="base", dockerfile="workloads/base/Dockerfile"),
            },
            base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
        ),
        push=False,
        host="linux/amd64",
    )

    document = yaml.safe_load(rendered)
    assert list(document) == ["services"]
    for build_block in (service["build"] for service in document["services"].values()):
        assert set(build_block) <= {
            "dockerfile",
            "platforms",
            "args",
            "tags",
            "additional_contexts",
            "cache_to",
            "cache_from",
        }


def test_render_bake_with_no_services_renders_an_empty_document():
    assert _rendered_services(render_bake(_render_config({}), push=False, host="linux/amd64")) == {}


def _nvidia_smi_present(name: str) -> str | None:
    return "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None


def _nothing_present(_name: str) -> str | None:
    return None


def _always_true(_command: str) -> bool:
    return True


def test_gpu_types_derive_the_concrete_families() -> None:
    assert bake_module.GPU_TYPES == ("cuda", "rocm")


def test_gpu_literal_derives_flat_over_the_families() -> None:
    # typer cannot annotate union types, so Gpu must flatten to one Literal.
    assert get_args(bake_module.Gpu) == ("auto", "none", *bake_module.GPU_TYPES)


class TestHostPlatform:
    def test_amd64_spellings_map_to_linux_amd64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for machine in ("x86_64", "AMD64"):
            monkeypatch.setattr(platform, "machine", lambda machine=machine: machine)

            assert bake_module.host_platform() == "linux/amd64"

    def test_arm64_spellings_map_to_linux_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for machine in ("aarch64", "ARM64"):
            monkeypatch.setattr(platform, "machine", lambda machine=machine: machine)

            assert bake_module.host_platform() == "linux/arm64"

    def test_other_architectures_refuse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "machine", lambda: "riscv64")

        with pytest.raises(RuntimeError, match="Unsupported host architecture"):
            bake_module.host_platform()


class TestDetectGpu:
    def test_nvidia_smi_yields_cuda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", _nvidia_smi_present)
        monkeypatch.setattr("docker_devkit.bake.bash_check", _always_true)

        gpu: bake_module.Gpu = bake_module.detect_gpu()

        assert gpu == "cuda"

    def test_no_gpu_host_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", _nothing_present)

        with pytest.raises(RuntimeError, match="Could not detect GPU"):
            bake_module.detect_gpu()
