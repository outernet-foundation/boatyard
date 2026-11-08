import json
import hashlib
import os
import platform
from pathlib import Path
from subprocess import CalledProcessError
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from boatyard.cli import app
from boatyard.verbs import build
from boatyard import build as build_module
from boatyard import images as images_module
from boatyard import lock as lock_module
from boatyard.config.lock import LOCK_PATH

DIGEST_A = "sha256:" + "a" * 64
HEAD_SHA = "1111111111111111111111111111111111111111"
MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
REPOSITORY = "ghcr.io/example"
LOCAL_REGISTRY = "localhost:5000"
TORCH_AXIS = {"TORCH_DEVICE": ["cuda", "rocm"]}

runner = CliRunner()


def _write_repo(
    root: Path,
    *,
    dockerfiles: dict[str, str] | None = None,
    base_images: dict[str, str] | None = None,
    charts: dict[str, str] | None = None,
    platforms: list[str] | None = None,
    repository: str = REPOSITORY,
    variants: dict[str, list[str]] | None = None,
) -> None:
    for service, text in (dockerfiles or {}).items():
        directory = root / "workloads" / service
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Dockerfile").write_text(text, encoding="utf-8")
    payload: dict[str, Any] = {
        "requires": ">=0",
        "repository": repository,
        "project_name": "demo",
        "platforms": platforms or ["linux/amd64"],
        "services": {"core": {"workloads": ["api.yaml"]}},
    }
    if base_images:
        payload["base_images"] = base_images
    if variants:
        payload["variants"] = variants
    if charts:
        payload["k8s"] = {"charts": charts}
        for chart in charts:
            values = root / "stack" / "score" / "k8s" / "values" / f"{chart}.yaml"
            values.parent.mkdir(parents=True, exist_ok=True)
            values.write_text("chart: {}\n", encoding="utf-8")
    (root / "boatyard.yml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    score = root / "stack" / "score"
    score.mkdir(parents=True, exist_ok=True)
    (score / "api.yaml").write_text("metadata:\n  name: api\ncontainers:\n  api: {}\n", encoding="utf-8")
    if (base_images or charts) and not (root / LOCK_PATH).exists():
        (root / LOCK_PATH).write_text("", encoding="utf-8")


EXTERNAL = "ARG PY_IMAGE\nFROM ${PY_IMAGE}\n"
FANNED = "ARG PY_IMAGE\nARG TORCH_DEVICE\nFROM ${PY_IMAGE}\n"
DEPENDENT = "FROM neural-networks-base\n"

DEFAULT_DOCKERFILES: dict[str, str] = {
    "api": EXTERNAL,
    "postgres": EXTERNAL,
    "seaweedfs": EXTERNAL,
    "neural-networks-base": FANNED,
    "localizer": DEPENDENT,
    "reconstructor": DEPENDENT,
}


def _bake_targets(commands: list[str]) -> list[str]:
    command = next(command for command in commands if command.startswith("docker buildx bake"))
    words = command.split()
    stance = "--load" if "--load" in words else "--push"
    return words[words.index(stance) + 1 :]


def _baked(target: str, *, refs: str, digest: str) -> dict[str, dict[str, str]]:
    return {target: {"image.name": refs, "containerimage.digest": digest}}


def _tagged_entry() -> dict[str, str]:
    return {"image.name": "registry/api:tree-abc", "containerimage.digest": DIGEST_A}


def _no_image_shas(root: Path, config: object) -> dict[str, str]:
    return {}


def _no_gc_limits() -> None:
    return None


def _amd64_host() -> str:
    return "linux/amd64"


def _fixed_index(command: str) -> str:
    assert command.startswith("crane manifest ")
    return json.dumps({
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": DIGEST_A,
                "size": 1234,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": DIGEST_A,
                "size": 1234,
                "platform": {"os": "linux", "architecture": "arm64"},
            },
        ]
    })


def _fixed_digest(reference: str) -> str:
    return DIGEST_A


def _fixed_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
    monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
    monkeypatch.setattr(lock_module, "install_crane", _no_gc_limits)


class ShelveRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        registry: str,
        project: str,
        platform: str,
        tag: str,
        source_directory: Path,
        paths: list[str],
        **kwargs: object,
    ) -> None:
        manifest = json.loads((source_directory / paths[0]).read_text(encoding="utf-8"))
        self.calls.append({
            "registry": registry,
            "project": project,
            "platform": platform,
            "tag": tag,
            "manifest": manifest,
        })


def _head_sha(command: str) -> str:
    assert command == "git rev-parse HEAD"
    return HEAD_SHA


class BakeRunner:
    def __init__(self, entries: dict[str, dict[str, str]] | None = None) -> None:
        self.entries = entries or {}
        self.commands: list[str] = []
        self.rendered = ""
        self.shelve = ShelveRecorder()

    def run(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("docker buildx bake"):
            words = command.split()
            bake_file = Path(words[words.index("-f") + 1])
            self.rendered = bake_file.read_text(encoding="utf-8")
            targets = _bake_targets([command])
            entries = {target: self.entries.get(target, _tagged_entry()) for target in targets}
            Path("metadata.json").write_text(json.dumps(entries), encoding="utf-8")


class MissingTargetRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("docker buildx bake"):
            Path("metadata.json").write_text(json.dumps({"other": _tagged_entry()}), encoding="utf-8")


class BakeEnvProbe:
    def __init__(self) -> None:
        self.uv_image: str | None = None

    def run(self, command: str) -> None:
        self.uv_image = os.environ.get("UV_IMAGE")
        Path("metadata.json").write_text(json.dumps({"api": _tagged_entry()}), encoding="utf-8")


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
    *,
    dockerfiles: dict[str, str] | None = None,
    base_images: dict[str, str] | None = None,
    platforms: list[str] | None = None,
    variants: dict[str, list[str]] | None = None,
) -> BakeRunner:
    recorder = BakeRunner()
    monkeypatch.setattr(build_module, "bash", recorder.run)
    monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
    monkeypatch.setattr(build_module, "check_gc_limits", _no_gc_limits)
    monkeypatch.setattr(build_module, "host_platform", _amd64_host)
    monkeypatch.setattr(lock_module, "install_crane", _no_gc_limits)
    monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
    monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
    monkeypatch.setattr(build_module, "push_build", recorder.shelve)
    monkeypatch.setattr(build_module, "install_oras", lambda: None)
    monkeypatch.setattr(build_module, "bash_output", _head_sha)
    monkeypatch.setenv("REGISTRY", REPOSITORY)
    monkeypatch.chdir(tmp_path)
    _write_repo(
        tmp_path,
        dockerfiles=dockerfiles or DEFAULT_DOCKERFILES,
        base_images=base_images,
        platforms=platforms,
        variants=variants,
    )

    result = runner.invoke(app, ["build", *arguments])

    assert result.exit_code == 0, result.output
    return recorder


class TestDefaultTargets:
    def test_defaults_are_every_service_default_variant(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(monkeypatch, tmp_path, [], variants=TORCH_AXIS)

        assert _bake_targets(recorder.commands) == [
            "api",
            "localizer-cuda",
            "neural-networks-base-cuda",
            "postgres",
            "reconstructor-cuda",
            "seaweedfs",
        ]

    def test_an_undeclared_axis_arg_leaves_plain_targets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(monkeypatch, tmp_path, [])

        assert _bake_targets(recorder.commands) == [
            "api",
            "localizer",
            "neural-networks-base",
            "postgres",
            "reconstructor",
            "seaweedfs",
        ]

    def test_foreign_arch_repo_refuses_bare_build(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(build_module, "bash", BakeRunner().run)
        monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
        monkeypatch.setattr(build_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_module, "host_platform", _amd64_host)
        monkeypatch.setattr(lock_module, "install_crane", _no_gc_limits)
        monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"zed-capture": EXTERNAL}, platforms=["linux/arm64"])

        result = runner.invoke(app, ["build"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "this machine's arch" in str(result.exception)
        assert "--targets" in str(result.exception)

    def test_foreign_arch_repo_builds_under_explicit_targets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--targets", "zed-capture"],
            dockerfiles={"zed-capture": EXTERNAL},
            platforms=["linux/arm64"],
        )

        assert _bake_targets(recorder.commands) == ["zed-capture"]

    def test_multiarch_repo_pins_the_host_platform_in_load_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch, tmp_path, [], dockerfiles={"api": EXTERNAL}, platforms=["linux/amd64", "linux/arm64"]
        )

        assert _bake_targets(recorder.commands) == ["api"]
        assert yaml.safe_load(recorder.rendered)["services"]["api"]["build"]["platforms"] == ["linux/amd64"]

    def test_bake_invokes_the_rendered_temp_file_without_set_strings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        recorder = _prepare(monkeypatch, tmp_path, [], dockerfiles={"api": EXTERNAL})

        command = recorder.commands[0]
        assert command.startswith("docker buildx bake -f ")
        assert "--set" not in command
        assert "metadata.json" in command

    def test_missing_config_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["build"])

        assert result.exit_code != 0
        assert isinstance(result.exception, FileNotFoundError)


class TestVariantSelection:
    def _invoke(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]) -> list[str]:
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            arguments,
            dockerfiles={"neural-networks-base": FANNED, "localizer": DEPENDENT, "postgres": EXTERNAL},
            variants=TORCH_AXIS,
        )
        return _bake_targets(recorder.commands)

    def test_a_variant_address_builds_that_variant(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--targets", "localizer/rocm"]) == ["localizer-rocm"]

    def test_a_bare_name_builds_an_axis_less_service(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--targets", "postgres"]) == ["postgres"]

    def test_a_bare_name_on_an_axis_service_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = self._refuse(monkeypatch, tmp_path, ["--targets", "localizer"])

        assert "localizer declares the variants ['cuda', 'rocm']" in result.output
        assert "localizer/<variant>" in result.output

    def test_the_suffixed_target_name_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = self._refuse(monkeypatch, tmp_path, ["--targets", "localizer-rocm"])

        assert "Unknown target 'localizer-rocm'" in result.output

    def test_repeated_addresses_ride_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._invoke(monkeypatch, tmp_path, ["--targets", "postgres postgres"]) == ["postgres", "postgres"]

    def test_an_unknown_variant_refuses_listing_the_declared_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result = self._refuse(monkeypatch, tmp_path, ["--targets", "localizer/tpu"])

        assert "Unknown variant 'tpu' for localizer" in result.output
        assert "['cuda', 'rocm']" in result.output

    def test_a_variant_on_an_unfanned_service_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = self._refuse(monkeypatch, tmp_path, ["--targets", "postgres/cuda"])

        assert "postgres declares no variants" in result.output

    def test_an_unknown_name_refuses_listing_the_vocabulary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = self._refuse(monkeypatch, tmp_path, ["--targets", "ghost"])

        assert "Unknown target 'ghost'" in result.output
        assert "localizer/cuda" in result.output
        assert "localizer/rocm" in result.output

    def _refuse(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]) -> Any:
        monkeypatch.setattr(build_module, "bash", BakeRunner().run)
        monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
        monkeypatch.setattr(build_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_module, "host_platform", _amd64_host)
        monkeypatch.setattr(lock_module, "install_crane", _no_gc_limits)
        monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.chdir(tmp_path)
        _write_repo(
            tmp_path,
            dockerfiles={"neural-networks-base": FANNED, "localizer": DEPENDENT, "postgres": EXTERNAL},
            variants=TORCH_AXIS,
        )

        result = runner.invoke(app, ["build", *arguments])

        assert result.exit_code != 0
        return result


class TestBuildKeysGuard:
    def test_build_without_derived_images_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(
            tmp_path,
            dockerfiles=None,
            base_images={"RATHOLE_IMAGE": "docker.io/outernet-foundation/rathole:0.11.0"},
            repository="ghcr.io/example/infra-rathole",
        )

        result = runner.invoke(app, ["build"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "no Dockerfile image derives" in str(result.exception)


class TestBakeFailure:
    def test_failed_bake_raises_with_the_rendered_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        def _fail(command: str) -> None:
            raise CalledProcessError(returncode=1, cmd=command)

        monkeypatch.setattr(build_module, "bash", _fail)
        monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
        monkeypatch.setattr(build_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_module, "host_platform", _amd64_host)
        monkeypatch.setattr(lock_module, "install_crane", _no_gc_limits)
        monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"api": EXTERNAL})

        result = runner.invoke(app, ["build"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "rendered bake file" in str(result.exception)
        assert f"{REPOSITORY}/api:latest" in str(result.exception)


class TestPushDestination:
    def test_bare_push_builds_for_the_config_repository(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--push", "--targets", "zed-capture"],
            dockerfiles={"zed-capture": EXTERNAL},
        )

        build_block = yaml.safe_load(recorder.rendered)["services"]["zed-capture"]["build"]
        assert build_block["tags"] == [
            f"{REPOSITORY}/zed-capture:latest",
            f"{REPOSITORY}/zed-capture:${{ZED_CAPTURE_SHA}}",
        ]
        assert build_block["cache_to"] == [
            f"type=registry,ref={REPOSITORY}/build-cache:zed-capture,mode=max,image-manifest=true,oci-mediatypes=true"
        ]
        assert build_block["cache_from"] == [f"type=registry,ref={REPOSITORY}/build-cache:zed-capture"]
        assert "--push" in recorder.commands[0].split()

    def test_an_address_push_re_roots_tags_without_cache_refs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--push-to", LOCAL_REGISTRY, "--targets", "zed-capture"],
            dockerfiles={"zed-capture": EXTERNAL},
        )

        build_block = yaml.safe_load(recorder.rendered)["services"]["zed-capture"]["build"]
        assert build_block["tags"] == [
            f"{LOCAL_REGISTRY}/zed-capture:latest",
            f"{LOCAL_REGISTRY}/zed-capture:${{ZED_CAPTURE_SHA}}",
        ]
        assert "cache_to" not in build_block
        assert "cache_from" not in build_block

    def test_push_leaves_the_metadata_file_in_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _prepare(monkeypatch, tmp_path, ["--push", "--targets", "zed-capture"], dockerfiles={"zed-capture": EXTERNAL})

        assert (tmp_path / "metadata.json").is_file()

    def test_a_target_with_no_tagged_references_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(build_module, "bash", BakeRunner(_baked("zed-capture", refs="", digest=DIGEST_A)).run)
        monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
        monkeypatch.setattr(build_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_module, "host_platform", _amd64_host)
        monkeypatch.setattr(lock_module, "install_crane", _no_gc_limits)
        monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"zed-capture": EXTERNAL})

        result = runner.invoke(app, ["build", "--push", "--targets", "zed-capture"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)

    def test_bake_output_not_covering_the_targets_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(build_module, "bash", MissingTargetRunner().run)
        monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
        monkeypatch.setattr(build_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_module, "host_platform", _amd64_host)
        monkeypatch.setattr(lock_module, "install_crane", _no_gc_limits)
        monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"zed-capture": EXTERNAL})

        result = runner.invoke(app, ["build", "--targets", "zed-capture"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "does not cover" in str(result.exception)

    def test_an_address_with_a_scheme_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._refuse_address(monkeypatch, tmp_path, ["--push-to", "http://localhost:5000"])

    def test_an_address_with_a_trailing_slash_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._refuse_address(monkeypatch, tmp_path, ["--push-to", "localhost:5000/"])

    def test_a_single_word_destination_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._refuse_address(monkeypatch, tmp_path, ["--push-to", "zed-capture"])

    def _refuse_address(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]) -> None:
        monkeypatch.setattr(build_module, "bash", BakeRunner().run)
        monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
        monkeypatch.setattr(build_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_module, "host_platform", _amd64_host)
        monkeypatch.setattr(lock_module, "install_crane", _no_gc_limits)
        monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"zed-capture": EXTERNAL})

        result = runner.invoke(app, ["build", *arguments])

        assert result.exit_code != 0
        assert "no scheme" in " ".join(result.output.split())


class TestDigestShelving:
    def test_a_repository_push_shelves_one_shard(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--push", "--targets", "zed-capture"],
            dockerfiles={"zed-capture": EXTERNAL},
        )

        assert len(recorder.shelve.calls) == 1
        call = recorder.shelve.calls[0]
        assert call["registry"] == f"{REPOSITORY}/builds"
        assert call["project"] == "images-digests"
        assert call["platform"] == "all"
        assert call["tag"] == f"sha-{HEAD_SHA}-{hashlib.sha256(b'zed-capture').hexdigest()[:8]}"
        assert call["manifest"] == {"zed-capture": {"ref": "registry/api", "digest": DIGEST_A, "tags": ["tree-abc"]}}

    def test_the_shard_key_covers_every_leg_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--push", "--targets", "api postgres"],
            dockerfiles={"api": EXTERNAL, "postgres": EXTERNAL},
        )

        expected_entry = {"ref": "registry/api", "digest": DIGEST_A, "tags": ["tree-abc"]}
        assert recorder.shelve.calls[0]["tag"] == f"sha-{HEAD_SHA}-{hashlib.sha256(b'api,postgres').hexdigest()[:8]}"
        assert recorder.shelve.calls[0]["manifest"] == {"api": expected_entry, "postgres": expected_entry}

    def test_a_repository_push_without_registry_env_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(build_module, "bash", BakeRunner().run)
        monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
        monkeypatch.setattr(build_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_module, "host_platform", _amd64_host)
        monkeypatch.setattr(lock_module, "install_crane", _no_gc_limits)
        monkeypatch.setattr(lock_module, "bash_output", _fixed_index)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
        monkeypatch.delenv("REGISTRY", raising=False)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles={"zed-capture": EXTERNAL})

        result = runner.invoke(app, ["build", "--push", "--targets", "zed-capture"])

        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit)

    def test_an_address_push_never_shelves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--push-to", LOCAL_REGISTRY, "--targets", "zed-capture"],
            dockerfiles={"zed-capture": EXTERNAL},
        )

        assert recorder.shelve.calls == []


class TestGoneFlags:
    def test_the_gpu_flag_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["build", "--gpu", "cuda"])

        assert result.exit_code != 0
        assert "No such option" in result.output

    def test_the_gpu_only_flag_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["build", "--gpu-only"])

        assert result.exit_code != 0
        assert "No such option" in result.output


LOCK_DOCKERFILES: dict[str, str] = {"api": EXTERNAL}


class TestBakeSeesLockValues:
    def test_bake_sees_mirror_prefixed_lock_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        probe = BakeEnvProbe()
        monkeypatch.setattr(build_module, "bash", probe.run)
        monkeypatch.setattr(build_module, "check_gc_limits", _no_gc_limits)
        monkeypatch.setattr(build_module, "host_platform", _amd64_host)
        _fixed_registry(monkeypatch)
        monkeypatch.setattr(images_module, "compute_image_shas", _no_image_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, dockerfiles=LOCK_DOCKERFILES, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})

        result = runner.invoke(app, ["build"])

        assert result.exit_code == 0, result.output
        assert probe.uv_image == f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}"


def _rendered_services(rendered: str) -> dict[str, Any]:
    return yaml.safe_load(rendered)["services"]


class TestRenderBakeDeterminism:
    def test_identical_configs_render_byte_identical_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _fixed_registry(monkeypatch)
        first = _prepare(
            monkeypatch,
            tmp_path,
            [],
            base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            variants=TORCH_AXIS,
        )
        second = _prepare(
            monkeypatch,
            tmp_path,
            [],
            base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            variants=TORCH_AXIS,
        )

        assert first.rendered == second.rendered


class TestRenderBakeTagDerivation:
    def test_unfanned_service_carries_one_target_with_latest_and_sha_tags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        recorder = _prepare(monkeypatch, tmp_path, [], dockerfiles={"api": EXTERNAL})

        services = _rendered_services(recorder.rendered)

        assert list(services) == ["api"]
        assert services["api"]["build"]["tags"] == [
            f"{REPOSITORY}/api:latest",
            f"{REPOSITORY}/api:${{API_SHA}}",
        ]

    def test_variant_axes_derive_per_variant_targets_sharing_one_sha_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        recorder = _prepare(monkeypatch, tmp_path, [], dockerfiles={"localizer": FANNED}, variants=TORCH_AXIS)

        services = _rendered_services(recorder.rendered)

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
    def test_every_base_image_key_is_injected_on_every_service(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _fixed_registry(monkeypatch)
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            [],
            dockerfiles={"api": EXTERNAL},
            base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15", "EXTRA_IMAGE": "alpine:3.20"},
        )

        services = _rendered_services(recorder.rendered)

        assert services["api"]["build"]["args"] == {
            "UV_BASE_IMAGE": "${UV_BASE_IMAGE:-}",
            "EXTRA_IMAGE": "${EXTRA_IMAGE:-}",
        }

    def test_sha_args_render_only_for_declaring_dockerfiles(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            [],
            dockerfiles={
                "api": EXTERNAL,
                "localizer": "ARG PY_IMAGE\nARG LOCALIZER_SHA\nFROM ${PY_IMAGE}\n",
            },
        )

        services = _rendered_services(recorder.rendered)

        assert "LOCALIZER_SHA" not in services["api"]["build"]["args"]
        assert services["localizer"]["build"]["args"] == {"LOCALIZER_SHA": "${LOCALIZER_SHA:?err}"}

    def test_declared_values_reach_the_rendered_axis_arg(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(monkeypatch, tmp_path, [], dockerfiles={"localizer": FANNED}, variants=TORCH_AXIS)

        services = _rendered_services(recorder.rendered)

        assert services["localizer-cuda"]["build"]["args"]["TORCH_DEVICE"] == "cuda"
        assert services["localizer-rocm"]["build"]["args"]["TORCH_DEVICE"] == "rocm"


class TestRenderBakeContexts:
    def test_contexts_render_target_prefixed_and_variant_matched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            [],
            dockerfiles={"neural-networks-base": FANNED, "localizer": DEPENDENT},
            variants=TORCH_AXIS,
        )

        services = _rendered_services(recorder.rendered)

        assert services["localizer-cuda"]["build"]["additional_contexts"] == {
            "neural-networks-base": "target:neural-networks-base-cuda"
        }
        assert services["localizer-rocm"]["build"]["additional_contexts"] == {
            "neural-networks-base": "target:neural-networks-base-rocm"
        }

    def test_unfanned_context_references_the_bare_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            [],
            dockerfiles={"app": "FROM base\n", "base": EXTERNAL},
        )

        services = _rendered_services(recorder.rendered)

        assert services["app"]["build"]["additional_contexts"] == {"base": "target:base"}


class TestRenderBakeCache:
    def test_push_mode_renders_registry_cache_refs_per_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--push"],
            dockerfiles={"localizer": FANNED},
            variants={"TORCH_DEVICE": ["cuda"]},
        )

        services = _rendered_services(recorder.rendered)

        assert services["localizer-cuda"]["build"]["cache_to"] == [
            (
                f"type=registry,ref={REPOSITORY}/build-cache:localizer-cuda,"
                "mode=max,image-manifest=true,oci-mediatypes=true"
            )
        ]
        assert services["localizer-cuda"]["build"]["cache_from"] == [
            f"type=registry,ref={REPOSITORY}/build-cache:localizer-cuda"
        ]

    def test_a_destination_re_roots_tags_without_cache_refs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--push-to", LOCAL_REGISTRY],
            dockerfiles={"localizer": FANNED},
            variants={"TORCH_DEVICE": ["cuda"]},
        )

        services = _rendered_services(recorder.rendered)

        build_block = services["localizer-cuda"]["build"]
        assert build_block["tags"] == [
            f"{LOCAL_REGISTRY}/localizer-cuda:latest",
            f"{LOCAL_REGISTRY}/localizer-cuda:${{LOCALIZER_SHA}}",
        ]
        assert "cache_to" not in build_block
        assert "cache_from" not in build_block

    def test_load_mode_renders_no_cache_refs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(monkeypatch, tmp_path, [], dockerfiles={"api": EXTERNAL})

        services = _rendered_services(recorder.rendered)

        assert "cache_to" not in services["api"]["build"]
        assert "cache_from" not in services["api"]["build"]


class TestRenderBakePlatforms:
    def test_multi_arch_declaration_collapses_to_the_host_in_load_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            [],
            dockerfiles={"api": EXTERNAL},
            platforms=["linux/amd64", "linux/arm64"],
        )

        services = _rendered_services(recorder.rendered)

        assert services["api"]["build"]["platforms"] == ["linux/amd64"]

    def test_multi_arch_declaration_rides_through_in_push_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--push"],
            dockerfiles={"api": EXTERNAL},
            platforms=["linux/amd64", "linux/arm64"],
        )

        services = _rendered_services(recorder.rendered)

        assert services["api"]["build"]["platforms"] == ["linux/amd64", "linux/arm64"]

    def test_single_declared_platform_rides_through_in_load_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = _prepare(
            monkeypatch,
            tmp_path,
            ["--targets", "zed-capture"],
            dockerfiles={"zed-capture": EXTERNAL},
            platforms=["linux/arm64"],
        )

        services = _rendered_services(recorder.rendered)

        assert services["zed-capture"]["build"]["platforms"] == ["linux/arm64"]


def test_render_bake_carries_only_bake_read_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fixed_registry(monkeypatch)
    recorder = _prepare(
        monkeypatch,
        tmp_path,
        [],
        dockerfiles={"api": "FROM base\nARG API_SHA\n", "base": EXTERNAL},
        base_images={"UV_BASE_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
    )

    document = yaml.safe_load(recorder.rendered)
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


class TestHostPlatform:
    def test_amd64_spellings_map_to_linux_amd64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for machine in ("x86_64", "AMD64"):
            monkeypatch.setattr(platform, "machine", lambda machine=machine: machine)

            assert build_module.host_platform() == "linux/amd64"

    def test_arm64_spellings_map_to_linux_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for machine in ("aarch64", "ARM64"):
            monkeypatch.setattr(platform, "machine", lambda machine=machine: machine)

            assert build_module.host_platform() == "linux/arm64"

    def test_other_architectures_refuse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "machine", lambda: "riscv64")

        with pytest.raises(ValueError, match="Unsupported host architecture"):
            build_module.host_platform()


class TestPushFlags:
    def _parse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> tuple[bool, str | None]:
        _write_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        recorded: list[tuple[bool, str | None]] = []

        def _record(push: bool = False, push_to: str | None = None, **kwargs: object) -> None:
            recorded.append((push, push_to))

        monkeypatch.setattr(build, "run_build", _record)

        result = runner.invoke(app, ["build", *arguments])

        assert result.exit_code == 0, result.output
        return recorded[0]

    def test_absent_flags_pass_bool_false_and_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._parse(tmp_path, monkeypatch, []) == (False, None)

    def test_bare_push_targets_the_config_repository(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._parse(tmp_path, monkeypatch, ["--push"]) == (True, None)

    def test_push_to_passes_the_address(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._parse(tmp_path, monkeypatch, ["--push-to", LOCAL_REGISTRY]) == (False, LOCAL_REGISTRY)

    def test_attached_push_to_passes_the_address(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        assert self._parse(tmp_path, monkeypatch, [f"--push-to={LOCAL_REGISTRY}"]) == (False, LOCAL_REGISTRY)

    def test_push_and_push_to_refuse_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(build, "run_build", _record_failure)

        result = runner.invoke(app, ["build", "--push", "--push-to", LOCAL_REGISTRY])

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


def _record_failure(**kwargs: object) -> None:
    raise AssertionError("run_build must not run")
