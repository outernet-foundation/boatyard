import base64
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from boatyard.cli import app
from boatyard.models import config_file as config_file_module
from boatyard import lock as lock_module
from boatyard.verbs.up import render_env_entries
from boatyard.models.lock_file import LOCK_PATH, LockFile
from boatyard.models.image import Image

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_E = "sha256:" + "e" * 64
MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"

LOKI_UPSTREAM = "ghcr.io/grafana/helm-charts/loki"
LOCK_BASE_IMAGES = {
    "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
    "API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
}

runner = CliRunner()


def _refuse_the_tree_walk(_self: Image, _root: Path) -> str:
    raise AssertionError("the lock verb never walks the source tree")


class DigestRecorder:
    def __init__(self) -> None:
        self.inspected: list[str] = []

    def resolve(self, reference: str) -> str:
        self.inspected.append(reference)
        return DIGEST_A


class IndexRegistry:
    # Answers `crane manifest` with one concrete child per platform at one digest.
    def __init__(self, platform_digest: str = DIGEST_B) -> None:
        self.inspected: list[str] = []
        self.children = [_child(platform_digest, "linux", "amd64"), _child(platform_digest, "linux", "arm64")]

    def bash_output(self, command: str) -> str:
        assert command.startswith("crane manifest ")
        self.inspected.append(command.removeprefix("crane manifest "))
        return json.dumps({"manifests": self.children})


class BundleRegistry:
    # Answers `crane manifest` with an artifact whose compose files ride as inline base64 layers.
    def __init__(self, compose_texts: list[str]) -> None:
        self.inspected: list[str] = []
        self.compose_texts = compose_texts

    def bash_output(self, command: str) -> str:
        assert command.startswith("crane manifest ")
        self.inspected.append(command.removeprefix("crane manifest "))
        return json.dumps({
            "layers": [
                {
                    "mediaType": "application/vnd.docker.compose.file+yaml",
                    "digest": DIGEST_A,
                    "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                }
                for text in self.compose_texts
            ]
        })


def _child(
    digest: str, os_name: str, architecture: str, annotations: dict[str, str] | None = None
) -> dict[str, object]:
    child: dict[str, object] = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": digest,
        "size": 1234,
        "platform": {"os": os_name, "architecture": architecture},
    }
    if annotations is not None:
        child["annotations"] = annotations
    return child


class ManifestOutput:
    def __init__(self, output: str) -> None:
        self.output = output

    def __call__(self, command: str) -> str:
        assert "crane manifest" in command
        return self.output


def _fixed_digest(reference: str) -> str:
    return DIGEST_A


def _structured_entry(reference: str, *, index_digest: str, platforms: dict[str, str]) -> dict[str, object]:
    return {
        "index": f"{reference}@{index_digest}",
        "platforms": {platform: f"{reference}@{digest}" for platform, digest in platforms.items()},
    }


BARRIER_MIRROR_BASE = f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20"


def _barrier_entry(platforms: list[str]) -> dict[str, object]:
    return _structured_entry(BARRIER_MIRROR_BASE, index_digest=DIGEST_A, platforms=dict.fromkeys(platforms, DIGEST_B))


def _write_repo(
    root: Path,
    *,
    base_images: dict[str, Any] | None = None,
    charts: dict[str, str] | None = None,
    platforms: list[str] | None = None,
    ref_services: dict[str, Any] | None = None,
    gateway: int | None = None,
) -> None:
    payload: dict[str, Any] = {"requires": ">=0", "project_name": "demo", "repository": "ghcr.io/org/demo"}
    if gateway is not None:
        payload["gateway"] = gateway
    if base_images:
        payload["platforms"] = platforms or ["linux/amd64"]
        payload["base_images"] = base_images
    if charts:
        payload["k8s"] = {"charts": charts}
        for chart in charts:
            values = root / "stack" / "score" / "k8s" / "values" / f"{chart}.yaml"
            values.parent.mkdir(parents=True, exist_ok=True)
            values.write_text("chart: {}\n", encoding="utf-8")
    caller_refs = ref_services is not None and bool(ref_services)
    if ref_services is None:
        ref_services = {"core": {"options": {"main": {"workloads": ["api.yaml"]}}}}
        payload["platforms"] = platforms or ["linux/amd64"]
    elif platforms:
        payload["platforms"] = platforms
    if ref_services:
        payload["project_name"] = "demo"
        payload["services"] = ref_services
        score = root / "stack" / "score"
        score.mkdir(parents=True, exist_ok=True)
        (score / "api.yaml").write_text("metadata:\n  name: api\ncontainers: {}\n", encoding="utf-8")
    (root / "boatyard.yml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    if (base_images or charts or caller_refs) and not (root / LOCK_PATH).exists():
        (root / LOCK_PATH).write_text("", encoding="utf-8")


def _read_lock(root: Path) -> dict[str, Any]:
    return yaml.safe_load((root / LOCK_PATH).read_text(encoding="utf-8"))


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
    *,
    base_images: dict[str, Any] | None = None,
    charts: dict[str, str] | None = None,
    platforms: list[str] | None = None,
    ref_services: dict[str, Any] | None = None,
) -> Any:
    monkeypatch.setattr(lock_module, "bash_output", IndexRegistry().bash_output)
    monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
    monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
    monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)
    monkeypatch.chdir(tmp_path)
    _write_repo(
        tmp_path,
        base_images=base_images,
        charts=charts,
        platforms=platforms,
        ref_services=ref_services,
    )
    return runner.invoke(app, ["lock", *arguments])


class TestSelfSkip:
    def test_skips_without_a_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert "no lock declarations to relock" in result.output
        assert not (tmp_path / LOCK_PATH).exists()

    def test_skips_without_declarations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)
        monkeypatch.setattr(lock_module, "bash_output", IndexRegistry().bash_output)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
        monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path)

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        barrier = f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20"
        assert _read_lock(tmp_path)["images"]["ALPINE_IMAGE"] == {
            "index": f"{barrier}@{DIGEST_A}",
            "platforms": {"linux/amd64": f"{barrier}@{DIGEST_B}"},
        }

        rechecked = runner.invoke(app, ["lock", "--check"])

        assert rechecked.exit_code == 0, rechecked.output


def _write_built_images(root: Path) -> None:
    for name, text in {
        "neural-networks-base": "ARG UV_IMAGE\nFROM ${UV_IMAGE}\n",
        "localizer": "ARG NEURAL_NETWORKS_BASE\nFROM ${NEURAL_NETWORKS_BASE}\n",
    }.items():
        directory = root / "workloads" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Dockerfile").write_text(text, encoding="utf-8")
    score = root / "stack" / "score"
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


BUILT_BASE_IMAGES: dict[str, Any] = {
    "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
    "NEURAL_NETWORKS_BASE": {"dockerfile": "workloads/neural-networks-base/Dockerfile"},
}


class TestImageRecording:
    def test_built_entries_are_never_recorded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_built_images(tmp_path)

        result = _invoke(
            monkeypatch,
            tmp_path,
            [],
            base_images=BUILT_BASE_IMAGES,
            ref_services={"core": {"options": {"main": {"workloads": ["localizer.yaml"]}}}},
        )

        assert result.exit_code == 0, result.output
        assert set(_read_lock(tmp_path)["images"]) == {"ALPINE_IMAGE", "UV_IMAGE"}

    def test_a_committed_built_entry_refuses_relock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_built_images(tmp_path)
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "platforms": ["linux/amd64"],
                "images": {
                    "NEURAL_NETWORKS_BASE": _structured_entry(
                        f"{MIRROR_PREFIX}/ghcr.io/org/demo/neural-networks-base:1",
                        index_digest=DIGEST_A,
                        platforms={"linux/amd64": DIGEST_A},
                    )
                },
            }),
            encoding="utf-8",
        )

        result = _invoke(
            monkeypatch,
            tmp_path,
            [],
            base_images=BUILT_BASE_IMAGES,
            ref_services={"core": {"options": {"main": {"workloads": ["localizer.yaml"]}}}},
        )

        assert result.exit_code != 0
        assert "records the built entries ['NEURAL_NETWORKS_BASE']" in str(result.exception)

    def test_records_the_index_and_one_manifest_per_declared_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result = _invoke(
            monkeypatch,
            tmp_path,
            [],
            base_images={"LOKI_IMAGE": "docker.io/grafana/loki:3.5.0"},
            platforms=["linux/amd64", "linux/arm64"],
        )

        assert result.exit_code == 0, result.output
        base = f"{MIRROR_PREFIX}/docker.io/grafana/loki:3.5.0"
        assert _read_lock(tmp_path) == {
            "platforms": ["linux/amd64", "linux/arm64"],
            "images": {
                "LOKI_IMAGE": {
                    "index": f"{base}@{DIGEST_A}",
                    "platforms": {"linux/amd64": f"{base}@{DIGEST_B}", "linux/arm64": f"{base}@{DIGEST_B}"},
                },
                "ALPINE_IMAGE": _barrier_entry(["linux/amd64", "linux/arm64"]),
            },
        }

    def test_records_declarations_under_the_mirror_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text("", encoding="utf-8")

        registry = IndexRegistry()
        monkeypatch.setattr(lock_module, "bash_output", registry.bash_output)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
        monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
        monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, base_images=LOCK_BASE_IMAGES)

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert registry.inspected == [
            "ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
            "docker.io/library/alpine:3.20",
        ]
        assert _read_lock(tmp_path) == {
            "platforms": ["linux/amd64"],
            "images": {
                "API_IMAGE": _structured_entry(
                    f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3",
                    index_digest=DIGEST_A,
                    platforms={"linux/amd64": DIGEST_B},
                ),
                "UV_IMAGE": _structured_entry(
                    f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15",
                    index_digest=DIGEST_A,
                    platforms={"linux/amd64": DIGEST_B},
                ),
                "ALPINE_IMAGE": _barrier_entry(["linux/amd64"]),
            },
        }

    def test_two_consecutive_writes_are_byte_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        for _ in range(2):
            result = _invoke(monkeypatch, tmp_path, [], base_images=LOCK_BASE_IMAGES)
            assert result.exit_code == 0, result.output

        first = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
        result = _invoke(monkeypatch, tmp_path, [], base_images=LOCK_BASE_IMAGES)
        assert result.exit_code == 0, result.output
        assert (tmp_path / LOCK_PATH).read_text(encoding="utf-8") == first

    def test_digest_pinned_declarations_pass_through_prefixed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        base = f"{MIRROR_PREFIX}/docker.io/library/neural@{DIGEST_A}"
        result = _invoke(
            monkeypatch, tmp_path, [], base_images={"NEURAL_IMAGE": f"docker.io/library/neural@{DIGEST_A}"}
        )

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "platforms": ["linux/amd64"],
            "images": {
                "NEURAL_IMAGE": {"index": base, "platforms": {"linux/amd64": base}},
                "ALPINE_IMAGE": _barrier_entry(["linux/amd64"]),
            },
        }

    def test_an_unchanged_declaration_freezes_the_whole_structured_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {
                    "UV_IMAGE": _structured_entry(base, index_digest=DIGEST_B, platforms={"linux/amd64": DIGEST_A}),
                    "ALPINE_IMAGE": _barrier_entry(["linux/amd64"]),
                }
            }),
            encoding="utf-8",
        )

        registry = IndexRegistry()
        monkeypatch.setattr(lock_module, "bash_output", registry.bash_output)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
        monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
        monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert registry.inspected == []
        assert _read_lock(tmp_path)["images"]["UV_IMAGE"] == _structured_entry(
            base, index_digest=DIGEST_B, platforms={"linux/amd64": DIGEST_A}
        )

    def test_a_declared_platform_set_change_breaks_the_freeze_and_re_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {
                    "UV_IMAGE": _structured_entry(base, index_digest=DIGEST_B, platforms={"linux/amd64": DIGEST_A}),
                    "ALPINE_IMAGE": _barrier_entry(["linux/amd64"]),
                }
            }),
            encoding="utf-8",
        )

        registry = IndexRegistry()
        monkeypatch.setattr(lock_module, "bash_output", registry.bash_output)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
        monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
        monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)
        monkeypatch.chdir(tmp_path)
        _write_repo(
            tmp_path,
            base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            platforms=["linux/amd64", "linux/arm64"],
        )

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert registry.inspected == ["ghcr.io/astral-sh/uv:0.12.15", "docker.io/library/alpine:3.20"]
        assert _read_lock(tmp_path)["images"]["UV_IMAGE"] == _structured_entry(
            base,
            index_digest=DIGEST_A,
            platforms={"linux/amd64": DIGEST_B, "linux/arm64": DIGEST_B},
        )

    def test_frozen_entries_resolve_again_only_on_upgrade(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {
                    "API_IMAGE": _structured_entry(
                        f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3",
                        index_digest=DIGEST_A,
                        platforms={"linux/amd64": DIGEST_B},
                    ),
                    "UV_IMAGE": _structured_entry(
                        f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15",
                        index_digest=DIGEST_A,
                        platforms={"linux/amd64": DIGEST_B},
                    ),
                    "ALPINE_IMAGE": _barrier_entry(["linux/amd64"]),
                }
            }),
            encoding="utf-8",
        )

        result = _invoke(monkeypatch, tmp_path, [], base_images=LOCK_BASE_IMAGES)
        assert result.exit_code == 0, result.output

        registry = IndexRegistry()
        monkeypatch.setattr(lock_module, "bash_output", registry.bash_output)
        result = runner.invoke(app, ["lock", "--upgrade"])

        assert result.exit_code == 0, result.output
        assert registry.inspected == [
            "ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
            "docker.io/library/alpine:3.20",
        ]

    def test_a_tag_bump_breaks_the_freeze_and_re_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {
                    "API_IMAGE": _structured_entry(
                        f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.2",
                        index_digest=DIGEST_A,
                        platforms={"linux/amd64": DIGEST_B},
                    )
                }
            }),
            encoding="utf-8",
        )

        result = _invoke(
            monkeypatch, tmp_path, [], base_images={"API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3"}
        )

        assert result.exit_code == 0, result.output
        entry = _read_lock(tmp_path)["images"]["API_IMAGE"]
        assert entry["index"].endswith(f"api:1.2.3@{DIGEST_A}")
        assert entry["platforms"]["linux/amd64"].endswith(f"api:1.2.3@{DIGEST_B}")

    def test_entries_whose_declarations_died_prune_by_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {
                    "DEAD_OPERATOR_IMAGE": _structured_entry(
                        f"{MIRROR_PREFIX}/quay.io/op/operator:1.0",
                        index_digest=DIGEST_B,
                        platforms={"linux/amd64": DIGEST_B},
                    ),
                    "RATHOLE_IMAGE": _structured_entry(
                        f"{MIRROR_PREFIX}/docker.io/x/rathole:0.11.0",
                        index_digest=DIGEST_B,
                        platforms={"linux/amd64": DIGEST_B},
                    ),
                }
            }),
            encoding="utf-8",
        )

        result = _invoke(monkeypatch, tmp_path, [], base_images={"RATHOLE_IMAGE": "docker.io/x/rathole:0.11.0"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "platforms": ["linux/amd64"],
            "images": {
                "RATHOLE_IMAGE": _structured_entry(
                    f"{MIRROR_PREFIX}/docker.io/x/rathole:0.11.0",
                    index_digest=DIGEST_B,
                    platforms={"linux/amd64": DIGEST_B},
                ),
                "ALPINE_IMAGE": _barrier_entry(["linux/amd64"]),
            },
        }


def _lock_against_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manifests: list[Any],
    platforms: list[str],
) -> Any:
    monkeypatch.setattr(lock_module, "bash_output", ManifestOutput(json.dumps({"manifests": manifests})))
    monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)
    monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
    monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)
    monkeypatch.chdir(tmp_path)
    _write_repo(tmp_path, platforms=platforms)
    return runner.invoke(app, ["lock"])


class TestPlatformManifestSelection:
    ALPINE_BASE = f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20"

    def test_selects_one_manifest_per_declared_platform(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        manifests = [
            _child(DIGEST_C, "linux", "amd64"),
            _child(DIGEST_D, "linux", "arm64"),
            _child(DIGEST_E, "unknown", "unknown", {"vnd.docker.reference.type": "attestation-manifest"}),
        ]

        result = _lock_against_index(monkeypatch, tmp_path, manifests, ["linux/amd64", "linux/arm64"])

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path)["images"]["ALPINE_IMAGE"]["platforms"] == {
            "linux/amd64": f"{self.ALPINE_BASE}@{DIGEST_C}",
            "linux/arm64": f"{self.ALPINE_BASE}@{DIGEST_D}",
        }

    def test_skips_attestation_entries_carrying_a_concrete_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        manifests = [
            _child(DIGEST_C, "linux", "amd64"),
            _child(DIGEST_E, "linux", "amd64", {"vnd.docker.reference.type": "attestation-manifest"}),
        ]

        result = _lock_against_index(monkeypatch, tmp_path, manifests, ["linux/amd64"])

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path)["images"]["ALPINE_IMAGE"]["platforms"] == {
            "linux/amd64": f"{self.ALPINE_BASE}@{DIGEST_C}",
        }

    def test_ignores_platformless_descriptors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        manifests = [
            _child(DIGEST_D, "linux", "arm64"),
            {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "f" * 64},
        ]

        result = _lock_against_index(monkeypatch, tmp_path, manifests, ["linux/arm64"])

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path)["images"]["ALPINE_IMAGE"]["platforms"] == {
            "linux/arm64": f"{self.ALPINE_BASE}@{DIGEST_D}",
        }

    def test_a_non_mapping_child_refuses_at_parse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = _lock_against_index(monkeypatch, tmp_path, ["not-a-descriptor"], ["linux/amd64"])

        assert result.exit_code == 1
        assert isinstance(result.exception, ValidationError)

    def test_a_digestless_child_refuses_at_parse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        manifests = [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {"os": "linux", "architecture": "amd64"},
            }
        ]

        result = _lock_against_index(monkeypatch, tmp_path, manifests, ["linux/amd64"])

        assert result.exit_code == 1
        assert isinstance(result.exception, ValidationError)

    def test_multiple_matches_refuse_naming_the_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        manifests = [_child(DIGEST_C, "linux", "amd64"), _child(DIGEST_D, "linux", "amd64")]

        result = _lock_against_index(monkeypatch, tmp_path, manifests, ["linux/amd64"])

        assert result.exit_code == 1
        assert "matches 2 upstream manifests, expected exactly one" in str(result.exception)

    def test_a_declared_platform_no_index_child_covers_refuses_naming_the_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result = _lock_against_index(monkeypatch, tmp_path, [_child(DIGEST_C, "linux", "amd64")], ["linux/arm64"])

        assert result.exit_code == 1
        assert "matches 0 upstream manifests, expected exactly one" in str(result.exception)

    def test_a_manifest_answer_records_uniformly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = _lock_against_index(monkeypatch, tmp_path, [], ["linux/amd64", "linux/arm64"])

        assert result.exit_code == 0, result.output
        entry = _read_lock(tmp_path)["images"]["ALPINE_IMAGE"]
        assert entry["index"] == f"{self.ALPINE_BASE}@{DIGEST_A}"
        assert entry["platforms"] == {
            "linux/amd64": f"{self.ALPINE_BASE}@{DIGEST_A}",
            "linux/arm64": f"{self.ALPINE_BASE}@{DIGEST_A}",
        }


class TestChartRecording:
    def test_tag_declared_chart_resolves_under_its_chart_lock_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result = _invoke(monkeypatch, tmp_path, [], charts={"loki": f"oci://{LOKI_UPSTREAM}:7.3.0"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "platforms": ["linux/amd64"],
            "images": {"ALPINE_IMAGE": _barrier_entry(["linux/amd64"])},
            "charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_A}"},
        }

    def test_digest_declared_chart_passes_through_unresolved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = _invoke(monkeypatch, tmp_path, [], charts={"loki": f"oci://{LOKI_UPSTREAM}@{DIGEST_B}"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "platforms": ["linux/amd64"],
            "images": {"ALPINE_IMAGE": _barrier_entry(["linux/amd64"])},
            "charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}@{DIGEST_B}"},
        }

    def test_unchanged_chart_declaration_copies_its_frozen_digest_forward(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {"ALPINE_IMAGE": _barrier_entry(["linux/amd64"])},
                "charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_B}"},
            }),
            encoding="utf-8",
        )

        result = _invoke(monkeypatch, tmp_path, [], charts={"loki": f"oci://{LOKI_UPSTREAM}:7.3.0"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "platforms": ["linux/amd64"],
            "images": {"ALPINE_IMAGE": _barrier_entry(["linux/amd64"])},
            "charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_B}"},
        }

    def test_chart_tag_bump_re_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {"ALPINE_IMAGE": _barrier_entry(["linux/amd64"])},
                "charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_B}"},
            }),
            encoding="utf-8",
        )

        result = _invoke(monkeypatch, tmp_path, [], charts={"loki": f"oci://{LOKI_UPSTREAM}:7.3.1"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "platforms": ["linux/amd64"],
            "images": {"ALPINE_IMAGE": _barrier_entry(["linux/amd64"])},
            "charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.1@{DIGEST_A}"},
        }


class TestBundleRecording:
    TUNNEL_RATHOLE = "oci://ghcr.io/org/repo/tunnel-rathole:0.3.1"
    RATHOLE_BASE = "ghcr.io/org/repo/tunnel-rathole:0.3.1"

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        arguments: list[str],
        *,
        committed: str = "",
        ref_services: dict[str, Any] | None = None,
        bundle_text: str = "services:\n  tunnel-rathole-rathole:\n    image: busybox:1.36\n",
        declares_routes: bool = False,
    ) -> tuple[Any, DigestRecorder, list[str]]:
        recorder = DigestRecorder()
        registry = BundleRegistry([bundle_text])
        monkeypatch.setattr(lock_module, "resolve_remote_digest", recorder.resolve)
        monkeypatch.setattr(lock_module, "bash_output", registry.bash_output)
        monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
        monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)
        monkeypatch.chdir(tmp_path)
        services = ref_services or {"tunnel": {"options": {"rathole": {"ref": self.TUNNEL_RATHOLE}}}}
        if declares_routes:
            caddy_declaration = "docker.io/library/caddy:2.8.4-alpine@" + DIGEST_A
            caddy_base = MIRROR_PREFIX + "/" + caddy_declaration
            _write_repo(
                tmp_path,
                gateway=8080,
                platforms=["linux/amd64"],
                ref_services=services,
            )
            if not committed:
                committed = yaml.safe_dump({
                    "images": {"CADDY_IMAGE": {"index": caddy_base, "platforms": {"linux/amd64": caddy_base}}}
                })
        else:
            _write_repo(tmp_path, ref_services=services)
        if committed:
            (tmp_path / LOCK_PATH).write_text(committed, encoding="utf-8")
        return runner.invoke(app, ["lock", *arguments]), recorder, registry.inspected

    def test_an_option_ref_resolves_under_its_qualified_bundle_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result, recorder, _ = self._invoke(monkeypatch, tmp_path, [])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == [self.RATHOLE_BASE]
        assert _read_lock(tmp_path) == {
            "bundles": {"TUNNEL_RATHOLE_BUNDLE": {"reference": f"{self.RATHOLE_BASE}@{DIGEST_A}"}}
        }

    def test_a_bare_entry_ref_resolves_under_its_own_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result, _, _ = self._invoke(
            monkeypatch,
            tmp_path,
            [],
            ref_services={"livekit": {"ref": "oci://ghcr.io/org/repo/livekit:0.4.0-dev.3"}},
        )

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "bundles": {"LIVEKIT_BUNDLE": {"reference": "ghcr.io/org/repo/livekit:0.4.0-dev.3@" + DIGEST_A}}
        }

    def test_bundle_entries_resolve_direct_never_mirror_prefixed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result, _, _ = self._invoke(monkeypatch, tmp_path, [])

        assert result.exit_code == 0, result.output
        [entry] = _read_lock(tmp_path)["bundles"].values()
        assert entry["reference"].startswith("ghcr.io/org/repo/")
        assert not entry["reference"].startswith(MIRROR_PREFIX)

    def test_slot_demands_record_under_the_bundle_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        bundle_text = (
            "services:\n  rathole:\n    image: busybox:1.36\n"
            "    environment:\n"
            "      Z_VAR: ${Z_REQUIRED}\n"
            "      A_VAR: ${A_REQUIRED:?err}\n"
            "      O_VAR: ${OPTIONAL:-x}\n"
            "      E_VAR: $$ESCAPED\n"
            "    configs:\n"
            "      - source: conf\n        target: /etc/conf\n"
            "configs:\n  conf:\n    content: |\n      slot=${Z_REQUIRED}\n"
        )

        result, _, _ = self._invoke(monkeypatch, tmp_path, [], bundle_text=bundle_text)

        assert result.exit_code == 0, result.output
        [entry] = _read_lock(tmp_path)["bundles"].values()
        assert entry["slots"] == {
            "Z_REQUIRED": {},
            "A_REQUIRED": {},
            "OPTIONAL": {"required": False},
        }

    def test_an_unresolved_route_upstream_refuses_naming_the_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result, _, read_references = self._invoke(
            monkeypatch,
            tmp_path,
            [],
            declares_routes=True,
            ref_services={
                "tunnel": {
                    "options": {
                        "rathole": {
                            "ref": self.TUNNEL_RATHOLE,
                            "routes": [{"path": "/*", "upstream": "webapp:3000"}],
                        }
                    }
                }
            },
        )

        assert result.exit_code == 1
        assert "references 'tunnel-rathole-webapp'" in str(result.exception) + result.output
        assert "'/*'" in str(result.exception) + result.output
        assert read_references == [f"{self.RATHOLE_BASE}@{DIGEST_A}"]

    def test_an_unresolved_port_owner_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result, _, _ = self._invoke(
            monkeypatch,
            tmp_path,
            [],
            declares_routes=True,
            ref_services={
                "tunnel": {
                    "options": {
                        "rathole": {
                            "ref": self.TUNNEL_RATHOLE,
                            "ports": [{"service": "database", "published": 5432, "target": 5432}],
                        }
                    }
                }
            },
        )

        assert result.exit_code == 1
        assert "references 'tunnel-rathole-database'" in str(result.exception) + result.output

    def test_a_frozen_digest_with_declared_routes_still_reads_and_validates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        caddy_declaration = "docker.io/library/caddy:2.8.4-alpine@" + DIGEST_B
        caddy_base = MIRROR_PREFIX + "/" + caddy_declaration
        committed = yaml.safe_dump({
            "images": {"CADDY_IMAGE": {"index": caddy_base, "platforms": {"linux/amd64": caddy_base}}},
            "bundles": {"TUNNEL_RATHOLE_BUNDLE": {"reference": f"{self.RATHOLE_BASE}@{DIGEST_B}"}},
        })

        result, recorder, read_references = self._invoke(
            monkeypatch,
            tmp_path,
            [],
            committed=committed,
            declares_routes=True,
            ref_services={
                "tunnel": {
                    "options": {
                        "rathole": {
                            "ref": self.TUNNEL_RATHOLE,
                            "routes": [{"path": "/*", "upstream": "rathole:8000"}],
                        }
                    }
                }
            },
        )

        assert result.exit_code == 0, result.output
        assert recorder.inspected == []
        assert read_references == [f"{self.RATHOLE_BASE}@{DIGEST_B}"]
        assert _read_lock(tmp_path)["bundles"] == {
            "TUNNEL_RATHOLE_BUNDLE": {"reference": f"{self.RATHOLE_BASE}@{DIGEST_B}"}
        }

    def test_an_unchanged_ref_copies_its_frozen_digest_forward(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        committed = yaml.safe_dump({
            "bundles": {"TUNNEL_RATHOLE_BUNDLE": {"reference": f"{self.RATHOLE_BASE}@{DIGEST_B}"}}
        })

        result, recorder, read_references = self._invoke(monkeypatch, tmp_path, [], committed=committed)

        assert result.exit_code == 0, result.output
        assert recorder.inspected == []
        assert read_references == [f"{self.RATHOLE_BASE}@{DIGEST_B}"]
        assert _read_lock(tmp_path) == {
            "bundles": {"TUNNEL_RATHOLE_BUNDLE": {"reference": f"{self.RATHOLE_BASE}@{DIGEST_B}"}}
        }

    def test_a_tag_bump_re_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        committed = yaml.safe_dump({
            "bundles": {"TUNNEL_RATHOLE_BUNDLE": {"reference": f"{self.RATHOLE_BASE}@{DIGEST_B}"}}
        })

        result, recorder, _ = self._invoke(
            monkeypatch,
            tmp_path,
            [],
            committed=committed,
            ref_services={"tunnel": {"options": {"rathole": {"ref": "oci://ghcr.io/org/repo/tunnel-rathole:0.3.2"}}}},
        )

        assert result.exit_code == 0, result.output
        assert recorder.inspected == ["ghcr.io/org/repo/tunnel-rathole:0.3.2"]
        assert _read_lock(tmp_path)["bundles"]["TUNNEL_RATHOLE_BUNDLE"]["reference"] == (
            "ghcr.io/org/repo/tunnel-rathole:0.3.2@" + DIGEST_A
        )

    def test_upgrade_re_resolves_frozen_bundle_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        committed = yaml.safe_dump({
            "bundles": {"TUNNEL_RATHOLE_BUNDLE": {"reference": f"{self.RATHOLE_BASE}@{DIGEST_B}"}}
        })

        result, recorder, _ = self._invoke(monkeypatch, tmp_path, ["--upgrade"], committed=committed)

        assert result.exit_code == 0, result.output
        assert recorder.inspected == [self.RATHOLE_BASE]
        assert (
            _read_lock(tmp_path)["bundles"]["TUNNEL_RATHOLE_BUNDLE"]["reference"] == f"{self.RATHOLE_BASE}@{DIGEST_A}"
        )

    def test_entries_whose_declarations_died_prune_by_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        committed = yaml.safe_dump({
            "bundles": {
                "GHOST_BUNDLE": {"reference": f"ghcr.io/org/repo/ghost:0.1.0@{DIGEST_B}"},
                "TUNNEL_RATHOLE_BUNDLE": {"reference": f"{self.RATHOLE_BASE}@{DIGEST_B}"},
            }
        })

        result, _, _ = self._invoke(monkeypatch, tmp_path, [], committed=committed)

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "bundles": {"TUNNEL_RATHOLE_BUNDLE": {"reference": f"{self.RATHOLE_BASE}@{DIGEST_B}"}}
        }


def _skip_crane_install() -> None:
    return None


class FixedBashOutput:
    def __init__(self, output: str) -> None:
        self.output = output

    def __call__(self, command: str) -> str:
        return self.output


class TestResolveRemoteDigest:
    def test_returns_the_crane_digest(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lock_module, "bash_output", FixedBashOutput(DIGEST_A + "\n"))
        monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
        assert lock_module.resolve_remote_digest("ghcr.io/org/repo/x:0.3.1") == DIGEST_A

    def test_refuses_output_that_is_not_one_digest(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lock_module, "bash_output", FixedBashOutput("Name: x\n"))
        monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
        with pytest.raises(ValueError, match="must be one sha256 digest"):
            lock_module.resolve_remote_digest("ghcr.io/org/repo/x:0.3.1")


class TestLockCheck:
    def _prepare(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)
        monkeypatch.setattr(lock_module, "bash_output", IndexRegistry().bash_output)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
        monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, base_images=LOCK_BASE_IMAGES)

    def test_fresh_lock_passes_and_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._prepare(monkeypatch, tmp_path)
        assert runner.invoke(app, ["lock"]).exit_code == 0
        before = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")

        result = runner.invoke(app, ["lock", "--check"])

        assert result.exit_code == 0, result.output
        assert (tmp_path / LOCK_PATH).read_text(encoding="utf-8") == before

    def test_stale_lock_exits_nonzero_and_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._prepare(monkeypatch, tmp_path)
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {
                    "UV_IMAGE": _structured_entry(
                        f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.11.0",
                        index_digest=DIGEST_B,
                        platforms={"linux/amd64": DIGEST_B},
                    )
                }
            }),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["lock", "--check"])

        assert result.exit_code == 1, result.output
        assert "image lock is stale" in str(result.exception)
        entry = _read_lock(tmp_path)["images"]["UV_IMAGE"]
        assert entry["index"].endswith(f"uv:0.11.0@{DIGEST_B}")


def test_build_no_longer_carries_the_lock_flags() -> None:
    result = runner.invoke(app, ["build", "--lock-only"])
    assert result.exit_code == 2

    result = runner.invoke(app, ["build", "--check"])
    assert result.exit_code == 2

    result = runner.invoke(app, ["build", "--upgrade"])
    assert result.exit_code == 2


def _selection_lock(platforms: list[str] | None = None) -> lock_module.LockFile:
    return lock_module.LockFile.model_validate({
        "platforms": platforms if platforms is not None else ["linux/amd64", "linux/arm64"],
        "images": {
            "LOKI_IMAGE": {
                "index": f"{MIRROR_PREFIX}/docker.io/grafana/loki:3.5.0@{DIGEST_A}",
                "platforms": {
                    "linux/amd64": f"{MIRROR_PREFIX}/docker.io/grafana/loki:3.5.0@{DIGEST_B}",
                    "linux/arm64": f"{MIRROR_PREFIX}/docker.io/grafana/loki:3.5.0@{DIGEST_C}",
                },
            }
        },
        "charts": {
            "GRAFANA_CHART": f"{MIRROR_PREFIX}/ghcr.io/grafana/chart@{DIGEST_A}",
        },
    })


class TestEntrySelection:
    def test_the_projection_carries_no_entry_for_unknown_names(self) -> None:
        assert "GHOST_IMAGE" not in _selection_lock().index_references()


class TestIndexReferences:
    def test_the_projection_without_a_platform_carries_index_references(self) -> None:
        entries = _selection_lock().index_references()

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_A}")
        assert entries["GRAFANA_CHART"].endswith(f"@{DIGEST_A}")


class TestManifestReferences:
    def test_manifest_references_select_platform_manifests_over_the_recorded_singleton(self) -> None:
        entries = _selection_lock(platforms=["linux/amd64"]).manifest_references(None)

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_B}")
        assert entries["GRAFANA_CHART"].endswith(f"@{DIGEST_A}")

    def test_manifest_references_select_the_platform_the_environment_names(self) -> None:
        entries = _selection_lock().manifest_references("linux/arm64")

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_C}")

    def test_the_env_render_is_sorted_and_stable(self) -> None:
        entries = _selection_lock(platforms=["linux/amd64"]).manifest_references(None)

        rendered = render_env_entries(entries)

        assert rendered == render_env_entries(entries)
        assert rendered.splitlines() == sorted(rendered.splitlines())
        assert rendered.endswith("\n")


class TestPlatformSelectionLaw:
    def test_a_singleton_selects_itself_without_reading_the_environment(self) -> None:
        entries = _selection_lock(platforms=["linux/amd64"]).manifest_references(None)

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_B}")

    def test_a_multi_platform_lock_selects_the_named_platform(self) -> None:
        entries = _selection_lock().manifest_references("linux/arm64")

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_C}")

    def test_a_multi_platform_lock_without_a_selection_refuses_listing_recorded(self) -> None:
        with pytest.raises(
            ValueError,
            match=r"platform must be named in boatyard\.values\.yaml \(the lock records \['linux/amd64', 'linux/arm64'\]\)",
        ):
            _selection_lock().manifest_references(None)

    def test_a_selection_outside_the_recorded_set_refuses_listing_recorded(self) -> None:
        with pytest.raises(
            ValueError,
            match=r"platform: 'linux/arm64' must be one of the recorded platforms \['linux/amd64'\]",
        ):
            _selection_lock(platforms=["linux/amd64"]).manifest_references("linux/arm64")

    def test_a_platform_less_lock_answers_none(self) -> None:
        charts_only = lock_module.LockFile(
            charts={"GRAFANA_CHART": f"{MIRROR_PREFIX}/ghcr.io/grafana/chart@{DIGEST_A}"}
        )

        entries = charts_only.manifest_references(None)

        assert entries == {"GRAFANA_CHART": f"{MIRROR_PREFIX}/ghcr.io/grafana/chart@{DIGEST_A}"}


class TestPairedLoad:
    def _config(self, tmp_path: Path) -> config_file_module.ConfigFile:
        return config_file_module.ConfigFile.load(tmp_path / "boatyard.yml")

    def _write_committed(self, tmp_path: Path, document: dict[str, Any]) -> None:
        (tmp_path / LOCK_PATH).write_text(yaml.safe_dump(document), encoding="utf-8")

    def test_a_platform_set_mismatch_refuses_naming_the_lock_verb(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})
        base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        self._write_committed(
            tmp_path,
            {
                "platforms": ["linux/amd64", "linux/arm64"],
                "images": {
                    "UV_IMAGE": _structured_entry(
                        base, index_digest=DIGEST_A, platforms={"linux/amd64": DIGEST_A, "linux/arm64": DIGEST_A}
                    )
                },
            },
        )

        with pytest.raises(
            ValueError,
            match=r"records platforms \['linux/amd64', 'linux/arm64'\] but boatyard\.yml declares \['linux/amd64'\]",
        ):
            LockFile.load(tmp_path / LOCK_PATH, self._config(tmp_path))

    def test_an_entry_diverging_from_the_recorded_set_refuses(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})
        base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        self._write_committed(
            tmp_path,
            {
                "platforms": ["linux/amd64"],
                "images": {
                    "UV_IMAGE": _structured_entry(
                        base, index_digest=DIGEST_A, platforms={"linux/amd64": DIGEST_A, "linux/arm64": DIGEST_B}
                    )
                },
            },
        )

        with pytest.raises(
            ValueError,
            match=r"image entry UV_IMAGE records platforms \['linux/amd64', 'linux/arm64'\] but the lock records "
            r"\['linux/amd64'\]",
        ):
            LockFile.load(tmp_path / LOCK_PATH, self._config(tmp_path))

    def test_a_missing_declared_entry_refuses_as_coverage(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})
        self._write_committed(
            tmp_path,
            {
                "platforms": ["linux/amd64"],
                "images": {
                    "OTHER_IMAGE": _structured_entry(
                        f"{MIRROR_PREFIX}/docker.io/x/other:1.0",
                        index_digest=DIGEST_A,
                        platforms={"linux/amd64": DIGEST_A},
                    )
                },
            },
        )

        with pytest.raises(ValueError, match=r"lacks the \['ALPINE_IMAGE', 'UV_IMAGE'\] image entries"):
            LockFile.load(tmp_path / LOCK_PATH, self._config(tmp_path))

    def test_an_orphan_entry_refuses_naming_the_lock_verb(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})
        base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        self._write_committed(
            tmp_path,
            {
                "platforms": ["linux/amd64"],
                "images": {
                    "UV_IMAGE": _structured_entry(base, index_digest=DIGEST_A, platforms={"linux/amd64": DIGEST_A}),
                    "ALPINE_IMAGE": _barrier_entry(["linux/amd64"]),
                    "GHOST_IMAGE": _structured_entry(
                        f"{MIRROR_PREFIX}/docker.io/x/ghost:1.0",
                        index_digest=DIGEST_A,
                        platforms={"linux/amd64": DIGEST_A},
                    ),
                },
            },
        )

        with pytest.raises(ValueError, match=r"no longer declares \(\['GHOST_IMAGE'\]\)"):
            LockFile.load(tmp_path / LOCK_PATH, self._config(tmp_path))

    def test_a_built_entry_in_the_committed_lock_refuses(self, tmp_path: Path) -> None:
        _write_repo(
            tmp_path,
            base_images=BUILT_BASE_IMAGES,
            ref_services={"core": {"options": {"main": {"workloads": ["localizer.yaml"]}}}},
        )
        _write_built_images(tmp_path)
        self._write_committed(
            tmp_path,
            {
                "platforms": ["linux/amd64"],
                "images": {
                    "NEURAL_NETWORKS_BASE": _structured_entry(
                        f"{MIRROR_PREFIX}/ghcr.io/org/demo/neural-networks-base:1",
                        index_digest=DIGEST_A,
                        platforms={"linux/amd64": DIGEST_A},
                    )
                },
            },
        )

        with pytest.raises(ValueError, match=r"records the built entries \['NEURAL_NETWORKS_BASE'\]"):
            LockFile.load(tmp_path / LOCK_PATH, self._config(tmp_path))

    def test_pairing_demands_only_the_pulled_entries(self, tmp_path: Path) -> None:
        _write_repo(
            tmp_path,
            base_images=BUILT_BASE_IMAGES,
            ref_services={"core": {"options": {"main": {"workloads": ["localizer.yaml"]}}}},
        )
        _write_built_images(tmp_path)
        base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        self._write_committed(
            tmp_path,
            {
                "platforms": ["linux/amd64"],
                "images": {
                    "UV_IMAGE": _structured_entry(base, index_digest=DIGEST_A, platforms={"linux/amd64": DIGEST_A}),
                    "ALPINE_IMAGE": _barrier_entry(["linux/amd64"]),
                },
            },
        )

        lock = LockFile.load(tmp_path / LOCK_PATH, self._config(tmp_path))

        assert set(lock.images) == {"ALPINE_IMAGE", "UV_IMAGE"}

    def test_a_missing_lock_in_a_platforms_repo_refuses_as_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["mirror"])

        assert result.exit_code == 1
        assert "records platforms [] but boatyard.yml declares ['linux/amd64']" in str(result.exception)

    def test_a_committed_lock_with_images_but_no_platforms_refuses(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})
        base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        self._write_committed(
            tmp_path,
            {
                "images": {
                    "UV_IMAGE": _structured_entry(base, index_digest=DIGEST_A, platforms={"linux/amd64": DIGEST_A})
                }
            },
        )

        with pytest.raises(ValueError, match=r"records platforms \[\] but boatyard.yml declares"):
            LockFile.load(tmp_path / LOCK_PATH, self._config(tmp_path))

    def test_a_missing_lock_in_a_bundles_only_repo_reads_as_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_repo(tmp_path, ref_services={"livekit": {"ref": "oci://ghcr.io/org/repo/livekit:0.4.0-dev.3"}})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["mirror"])

        assert result.exit_code == 1
        assert "nothing is declared to mirror" in str(result.exception)

    def test_load_reads_a_missing_lock_as_the_empty_paired_lock(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, ref_services={"livekit": {"ref": "oci://ghcr.io/org/repo/livekit:0.4.0-dev.3"}})

        lock = LockFile.load(tmp_path / LOCK_PATH, self._config(tmp_path))

        assert lock.images == {} and lock.charts == {} and lock.bundles == {}

    def test_relock_consumes_a_drifted_committed_lock_as_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})
        base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        self._write_committed(
            tmp_path,
            {
                "images": {
                    "UV_IMAGE": _structured_entry(base, index_digest=DIGEST_A, platforms={"linux/amd64": DIGEST_A})
                },
                "charts": {"GHOST_CHART": f"{MIRROR_PREFIX}/ghcr.io/x/ghost@{DIGEST_A}"},
            },
        )
        monkeypatch.setattr(lock_module, "bash_output", IndexRegistry().bash_output)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
        monkeypatch.setattr(lock_module, "install_crane", _skip_crane_install)
        monkeypatch.setattr(Image, "compute_sha", _refuse_the_tree_walk)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "platforms": ["linux/amd64"],
            "images": {
                "UV_IMAGE": _structured_entry(base, index_digest=DIGEST_A, platforms={"linux/amd64": DIGEST_A}),
                "ALPINE_IMAGE": _barrier_entry(["linux/amd64"]),
            },
        }
