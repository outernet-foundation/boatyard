import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from boatyard.cli import app
from boatyard import config as config_module
from boatyard import dockerfiles as dockerfiles_module
from boatyard import lock as lock_module
from boatyard.config_lock import LOCK_PATH
from boatyard.lock import UpstreamManifests

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


def _no_service_shas(_root: Path, _service_builds: object) -> dict[str, str]:
    return {}


class DigestRecorder:
    def __init__(self) -> None:
        self.inspected: list[str] = []

    def resolve(self, reference: str) -> str:
        self.inspected.append(reference)
        return DIGEST_A


class ManifestRecorder:
    def __init__(self, index_digest: str = DIGEST_A, platform_digest: str = DIGEST_B) -> None:
        self.inspected: list[str] = []
        self.index_digest = index_digest
        self.platform_digest = platform_digest

    def resolve(self, reference: str, platforms: list[str], subject: str) -> UpstreamManifests:
        self.inspected.append(reference)
        return UpstreamManifests(self.index_digest, dict.fromkeys(platforms, self.platform_digest))


def _inspect_output_with_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return f"Name: x\nDigest: {DIGEST_A}\n"


def _inspect_output_without_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return "Name: x\n"


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


class RawInspectOutput:
    def __init__(self, output: str) -> None:
        self.output = output

    def __call__(self, command: str) -> str:
        assert "imagetools inspect" in command
        return self.output


def _fixed_digest(reference: str) -> str:
    return DIGEST_A


def _structured_entry(reference: str, *, index_digest: str, platforms: dict[str, str]) -> dict[str, object]:
    return {
        "index": f"{reference}@{index_digest}",
        "platforms": {platform: f"{reference}@{digest}" for platform, digest in platforms.items()},
    }


def _write_repo(
    root: Path,
    *,
    base_images: dict[str, str] | None = None,
    charts: dict[str, str] | None = None,
    platforms: list[str] | None = None,
) -> None:
    payload: dict[str, Any] = {"requires": ">=0"}
    if base_images:
        payload["platforms"] = platforms or ["linux/amd64"]
        payload["base_images"] = base_images
    if charts:
        payload["k8s"] = {"charts": charts}
        for chart in charts:
            values = root / "stack" / "score" / "k8s" / "values" / f"{chart}.yaml"
            values.parent.mkdir(parents=True, exist_ok=True)
            values.write_text("chart: {}\n", encoding="utf-8")
    (root / "docker-devkit.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    if (base_images or charts) and not (root / LOCK_PATH).exists():
        (root / LOCK_PATH).write_text("", encoding="utf-8")


def _read_lock(root: Path) -> dict[str, Any]:
    return yaml.safe_load((root / LOCK_PATH).read_text(encoding="utf-8"))


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
    *,
    base_images: dict[str, str] | None = None,
    charts: dict[str, str] | None = None,
    platforms: list[str] | None = None,
) -> Any:
    recorder = ManifestRecorder()
    monkeypatch.setattr(lock_module, "resolve_remote_manifests", recorder.resolve)
    monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
    monkeypatch.setattr(dockerfiles_module, "compute_service_shas", _no_service_shas)
    monkeypatch.chdir(tmp_path)
    _write_repo(tmp_path, base_images=base_images, charts=charts, platforms=platforms)
    return runner.invoke(app, ["lock", *arguments])


class TestSelfSkip:
    def test_skips_without_a_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert "no lock declarations to relock" in result.output
        assert not (tmp_path / LOCK_PATH).exists()

    def test_skips_without_declarations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(dockerfiles_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path)

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert "nothing to relock" in result.output
        assert not (tmp_path / LOCK_PATH).exists()

    def test_check_self_skips_where_no_declarations_exist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(dockerfiles_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path)

        result = runner.invoke(app, ["lock", "--check"])

        assert result.exit_code == 0, result.output
        assert "nothing to relock" in result.output


class TestImageRecording:
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
            "images": {
                "LOKI_IMAGE": {
                    "index": f"{base}@{DIGEST_A}",
                    "platforms": {"linux/amd64": f"{base}@{DIGEST_B}", "linux/arm64": f"{base}@{DIGEST_B}"},
                }
            }
        }

    def test_records_declarations_under_the_mirror_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text("", encoding="utf-8")

        recorder = ManifestRecorder()
        monkeypatch.setattr(lock_module, "resolve_remote_manifests", recorder.resolve)
        monkeypatch.setattr(dockerfiles_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, base_images=LOCK_BASE_IMAGES)

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == [
            "ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
        ]
        assert _read_lock(tmp_path) == {
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
            }
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
        base = f"{MIRROR_PREFIX}/docker.io/library/alpine@{DIGEST_A}"
        result = _invoke(
            monkeypatch, tmp_path, [], base_images={"ALPINE_IMAGE": f"docker.io/library/alpine@{DIGEST_A}"}
        )

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {"images": {"ALPINE_IMAGE": {"index": base, "platforms": {"linux/amd64": base}}}}

    def test_an_unchanged_declaration_freezes_the_whole_structured_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {
                    "UV_IMAGE": _structured_entry(base, index_digest=DIGEST_B, platforms={"linux/amd64": DIGEST_A})
                }
            }),
            encoding="utf-8",
        )

        recorder = ManifestRecorder()
        monkeypatch.setattr(lock_module, "resolve_remote_manifests", recorder.resolve)
        monkeypatch.setattr(dockerfiles_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == []
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
                    "UV_IMAGE": _structured_entry(base, index_digest=DIGEST_B, platforms={"linux/amd64": DIGEST_A})
                }
            }),
            encoding="utf-8",
        )

        recorder = ManifestRecorder()
        monkeypatch.setattr(lock_module, "resolve_remote_manifests", recorder.resolve)
        monkeypatch.setattr(dockerfiles_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(
            tmp_path,
            base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            platforms=["linux/amd64", "linux/arm64"],
        )

        result = runner.invoke(app, ["lock"])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == ["ghcr.io/astral-sh/uv:0.12.15"]
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
                }
            }),
            encoding="utf-8",
        )

        result = _invoke(monkeypatch, tmp_path, [], base_images=LOCK_BASE_IMAGES)
        assert result.exit_code == 0, result.output

        recorder = ManifestRecorder()
        monkeypatch.setattr(lock_module, "resolve_remote_manifests", recorder.resolve)
        result = runner.invoke(app, ["lock", "--upgrade"])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == [
            "ghcr.io/astral-sh/uv:0.12.15",
            "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
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
            "images": {
                "RATHOLE_IMAGE": _structured_entry(
                    f"{MIRROR_PREFIX}/docker.io/x/rathole:0.11.0",
                    index_digest=DIGEST_B,
                    platforms={"linux/amd64": DIGEST_B},
                )
            }
        }


class TestResolveRemoteDigest:
    def test_should_return_parsed_digest(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lock_module, "bash_output", _inspect_output_with_digest)
        assert lock_module.resolve_remote_digest("docker.io/library/alpine:3.20") == DIGEST_A

    def test_should_raise_when_digest_missing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lock_module, "bash_output", _inspect_output_without_digest)
        with pytest.raises(ValueError, match="must carry a Digest line"):
            lock_module.resolve_remote_digest("docker.io/library/alpine:3.20")


class TestSelectPlatformManifests:
    def test_selects_one_manifest_per_declared_platform(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({
            "manifests": [
                _child(DIGEST_C, "linux", "amd64"),
                _child(DIGEST_D, "linux", "arm64"),
                _child(DIGEST_E, "unknown", "unknown", {"vnd.docker.reference.type": "attestation-manifest"}),
            ],
        })
        monkeypatch.setattr(lock_module, "bash_output", RawInspectOutput(raw))
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)

        resolved = lock_module.resolve_remote_manifests(
            "docker.io/library/alpine:3.20", ["linux/amd64", "linux/arm64"], "entry X"
        )

        assert resolved.platforms == {"linux/amd64": DIGEST_C, "linux/arm64": DIGEST_D}

    def test_skips_attestation_entries_carrying_a_concrete_platform(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({
            "manifests": [
                _child(DIGEST_C, "linux", "amd64"),
                _child(DIGEST_E, "linux", "amd64", {"vnd.docker.reference.type": "attestation-manifest"}),
            ],
        })
        monkeypatch.setattr(lock_module, "bash_output", RawInspectOutput(raw))
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)

        resolved = lock_module.resolve_remote_manifests("docker.io/library/alpine:3.20", ["linux/amd64"], "entry X")

        assert resolved.platforms == {"linux/amd64": DIGEST_C}

    def test_ignores_variant_fields_and_platformless_descriptors(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({
            "manifests": [
                _child(DIGEST_D, "linux", "arm64"),
                {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "f" * 64},
                "not-a-descriptor",
            ],
        })
        monkeypatch.setattr(lock_module, "bash_output", RawInspectOutput(raw))
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)

        resolved = lock_module.resolve_remote_manifests("docker.io/library/alpine:3.20", ["linux/arm64"], "entry X")

        assert resolved.platforms == {"linux/arm64": DIGEST_D}

    def test_multiple_matches_refuse_naming_the_entry(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({"manifests": [_child(DIGEST_C, "linux", "amd64"), _child(DIGEST_D, "linux", "amd64")]})
        monkeypatch.setattr(lock_module, "bash_output", RawInspectOutput(raw))
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)

        with pytest.raises(ValueError, match=f"entry X.*multiple upstream manifests.*{DIGEST_C}, {DIGEST_D}"):
            lock_module.resolve_remote_manifests("docker.io/library/alpine:3.20", ["linux/amd64"], "entry X")

    def test_a_declared_platform_no_index_child_covers_refuses_naming_the_entry(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({"manifests": [_child(DIGEST_C, "linux", "amd64")]})
        monkeypatch.setattr(lock_module, "bash_output", RawInspectOutput(raw))
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)

        with pytest.raises(ValueError, match=r"entry X.*no linux/arm64 manifest.*index platforms: \['linux/amd64'\]"):
            lock_module.resolve_remote_manifests("docker.io/library/alpine:3.20", ["linux/arm64"], "entry X")


class TestResolveRemoteManifests:
    def test_resolves_the_index_and_selects_declared_platforms(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [_child(DIGEST_C, "linux", "amd64"), _child(DIGEST_D, "linux", "arm64")],
        })
        monkeypatch.setattr(lock_module, "bash_output", RawInspectOutput(raw))
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)

        resolved = lock_module.resolve_remote_manifests("docker.io/library/alpine:3.20", ["linux/arm64"], "entry X")

        assert resolved.index_digest == DIGEST_A
        assert resolved.platforms == {"linux/arm64": DIGEST_D}

    def test_a_manifest_reference_records_uniformly(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {}})
        monkeypatch.setattr(lock_module, "bash_output", RawInspectOutput(raw))
        monkeypatch.setattr(lock_module, "resolve_remote_digest", _fixed_digest)

        resolved = lock_module.resolve_remote_manifests(
            "docker.io/library/golang:1.24", ["linux/amd64", "linux/arm64"], "entry X"
        )

        assert resolved.index_digest == DIGEST_A
        assert resolved.platforms == {"linux/amd64": DIGEST_A, "linux/arm64": DIGEST_A}


class TestChartRecording:
    def test_tag_declared_chart_resolves_under_its_chart_lock_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result = _invoke(monkeypatch, tmp_path, [], charts={"loki": f"oci://{LOKI_UPSTREAM}:7.3.0"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {"charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_A}"}}

    def test_digest_declared_chart_passes_through_unresolved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = _invoke(monkeypatch, tmp_path, [], charts={"loki": f"oci://{LOKI_UPSTREAM}@{DIGEST_B}"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {"charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}@{DIGEST_B}"}}

    def test_unchanged_chart_declaration_copies_its_frozen_digest_forward(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({"charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_B}"}}),
            encoding="utf-8",
        )

        result = _invoke(monkeypatch, tmp_path, [], charts={"loki": f"oci://{LOKI_UPSTREAM}:7.3.0"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {"charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_B}"}}

    def test_chart_tag_bump_re_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({"charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.0@{DIGEST_B}"}}),
            encoding="utf-8",
        )

        result = _invoke(monkeypatch, tmp_path, [], charts={"loki": f"oci://{LOKI_UPSTREAM}:7.3.1"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {"charts": {"LOKI_CHART": f"{MIRROR_PREFIX}/{LOKI_UPSTREAM}:7.3.1@{DIGEST_A}"}}


class TestLockCheck:
    def _prepare(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(dockerfiles_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(lock_module, "resolve_remote_manifests", ManifestRecorder().resolve)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
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


def _selection_lock() -> lock_module.ConfigLock:
    return lock_module.ConfigLock.model_validate({
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


def _selection_config(**sections: object) -> config_module.Config:
    payload: dict[str, object] = {"requires": ">=0"}
    payload.update(sections)
    return config_module.Config.model_validate(payload)


class TestEntrySelection:
    def test_the_projection_refuses_a_platform_an_entry_never_recorded(self) -> None:
        with pytest.raises(
            ValueError, match=r"LOKI_IMAGE records no linux/ppc64le platform manifest.*recorded platforms"
        ):
            lock_module.env_entries(_selection_lock(), "linux/ppc64le")

    def test_the_projection_carries_no_entry_for_unknown_names(self) -> None:
        assert "GHOST_IMAGE" not in lock_module.env_entries(_selection_lock())


class TestEnvProjection:
    def test_the_projection_without_a_platform_carries_index_references(self) -> None:
        entries = lock_module.env_entries(_selection_lock())

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_A}")
        assert entries["GRAFANA_CHART"].endswith(f"@{DIGEST_A}")

    def test_the_projection_with_a_platform_carries_platform_manifests(self) -> None:
        entries = lock_module.env_entries(_selection_lock(), "linux/amd64")

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_B}")
        assert entries["GRAFANA_CHART"].endswith(f"@{DIGEST_A}")


class TestComposeProjection:
    def test_compose_entries_select_platform_manifests_over_the_declared_singleton(self) -> None:
        config = _selection_config(platforms=["linux/amd64"])

        entries = lock_module.compose_env_entries(_selection_lock(), config, {})

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_B}")
        assert entries["GRAFANA_CHART"].endswith(f"@{DIGEST_A}")

    def test_compose_entries_select_the_platform_the_environment_names(self) -> None:
        config = _selection_config(platforms=["linux/amd64", "linux/arm64"])

        entries = lock_module.compose_env_entries(_selection_lock(), config, {"PLATFORM": "linux/arm64"})

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_C}")

    def test_compose_entries_refuse_image_entries_without_declared_platforms(self) -> None:
        config = _selection_config()

        with pytest.raises(ValueError, match=r"records image entries but docker-devkit\.yaml declares no platforms"):
            lock_module.compose_env_entries(_selection_lock(), config, {})

    def test_the_env_render_is_sorted_and_stable(self) -> None:
        entries = lock_module.compose_env_entries(_selection_lock(), _selection_config(platforms=["linux/amd64"]), {})

        rendered = lock_module.render_env_entries(entries)

        assert rendered == lock_module.render_env_entries(entries)
        assert rendered.splitlines() == sorted(rendered.splitlines())
        assert rendered.endswith("\n")


class TestPlatformSelectionLaw:
    def test_a_singleton_selects_itself_without_reading_the_environment(self) -> None:
        config = _selection_config(platforms=["linux/amd64"])

        entries = lock_module.compose_env_entries(_selection_lock(), config, {"PLATFORM": ""})

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_B}")

    def test_a_multi_platform_repo_selects_the_named_platform(self) -> None:
        config = _selection_config(platforms=["linux/amd64", "linux/arm64"])

        entries = lock_module.compose_env_entries(_selection_lock(), config, {"PLATFORM": "linux/arm64"})

        assert entries["LOKI_IMAGE"].endswith(f"@{DIGEST_C}")

    def test_a_multi_platform_repo_without_a_selection_refuses_listing_declared(self) -> None:
        config = _selection_config(platforms=["linux/amd64", "linux/arm64"])

        with pytest.raises(
            ValueError, match=r"platforms \['linux/amd64', 'linux/arm64'\] are declared.*PLATFORM= must be named"
        ):
            lock_module.compose_env_entries(_selection_lock(), config, {})

    def test_an_undeclared_selection_refuses_listing_declared(self) -> None:
        config = _selection_config(platforms=["linux/amd64"])

        with pytest.raises(
            ValueError,
            match=r"PLATFORM='linux/arm64' must be one of the declared platforms \['linux/amd64'\]",
        ):
            lock_module.compose_env_entries(_selection_lock(), config, {"PLATFORM": "linux/arm64"})

    def test_a_platform_less_declaring_repo_answers_none(self) -> None:
        charts_only = lock_module.ConfigLock(
            charts={"GRAFANA_CHART": f"{MIRROR_PREFIX}/ghcr.io/grafana/chart@{DIGEST_A}"}
        )

        entries = lock_module.compose_env_entries(charts_only, _selection_config(), {})

        assert entries == {"GRAFANA_CHART": f"{MIRROR_PREFIX}/ghcr.io/grafana/chart@{DIGEST_A}"}
