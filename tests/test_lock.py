from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from docker_devkit import config as config_module
from docker_devkit import lock as lock_module
from docker_devkit.config import LOCK_PATH
from docker_devkit.image_refs import UpstreamManifests
from docker_devkit.verbs import build, lock

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
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
    monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
    monkeypatch.chdir(tmp_path)
    _write_repo(tmp_path, base_images=base_images, charts=charts, platforms=platforms)
    return runner.invoke(lock.app, arguments)


class TestSelfSkip:
    def test_skips_without_a_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(lock.app, [])

        assert result.exit_code == 0, result.output
        assert "no lock declarations to relock" in result.output
        assert not (tmp_path / LOCK_PATH).exists()

    def test_skips_without_declarations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path)

        result = runner.invoke(lock.app, [])

        assert result.exit_code == 0, result.output
        assert "nothing to relock" in result.output
        assert not (tmp_path / LOCK_PATH).exists()

    def test_check_self_skips_where_no_declarations_exist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path)

        result = runner.invoke(lock.app, ["--check"])

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
        monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, base_images=LOCK_BASE_IMAGES)

        result = runner.invoke(lock.app, [])

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
        monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})

        result = runner.invoke(lock.app, [])

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
        monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(
            tmp_path,
            base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
            platforms=["linux/amd64", "linux/arm64"],
        )

        result = runner.invoke(lock.app, [])

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
        result = runner.invoke(lock.app, ["--upgrade"])

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


class TestCutover:
    def test_check_over_an_env_format_lock_fails_with_the_full_format_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / LOCK_PATH).write_text(
            f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}\n", encoding="utf-8"
        )
        result = _invoke(monkeypatch, tmp_path, ["--check"], base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})

        assert result.exit_code == 1, result.output
        assert "re-resolving every entry" in result.output
        assert "+images:" in result.output
        assert "+    platforms:" in result.output
        assert "image lock is stale" in result.output
        assert (tmp_path / LOCK_PATH).read_text(encoding="utf-8").startswith("UV_IMAGE=")

    def test_write_over_an_env_format_lock_re_resolves_and_rewrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / LOCK_PATH).write_text(
            f"UV_IMAGE={MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}\n", encoding="utf-8"
        )
        result = _invoke(monkeypatch, tmp_path, [], base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})

        assert result.exit_code == 0, result.output
        assert "re-resolving every entry" in result.output
        assert _read_lock(tmp_path) == {
            "images": {
                "UV_IMAGE": _structured_entry(
                    f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15",
                    index_digest=DIGEST_A,
                    platforms={"linux/amd64": DIGEST_B},
                )
            }
        }

    def test_a_phase_two_structured_lock_without_platforms_re_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Entries written before the platform-resolving writer carry no platforms map;
        # the freeze key covers the platform set, so the cutover re-resolves them.
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {"UV_IMAGE": {"index": f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}"}}
            }),
            encoding="utf-8",
        )

        recorder = ManifestRecorder()
        monkeypatch.setattr(lock_module, "resolve_remote_manifests", recorder.resolve)
        monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, base_images={"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"})

        result = runner.invoke(lock.app, [])

        assert result.exit_code == 0, result.output
        assert recorder.inspected == ["ghcr.io/astral-sh/uv:0.12.15"]
        assert _read_lock(tmp_path)["images"]["UV_IMAGE"]["platforms"] == {
            "linux/amd64": f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_B}"
        }


class TestLockCheck:
    def _prepare(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
        monkeypatch.setattr(lock_module, "resolve_remote_manifests", ManifestRecorder().resolve)
        monkeypatch.setattr(lock_module, "resolve_remote_digest", DigestRecorder().resolve)
        monkeypatch.chdir(tmp_path)
        _write_repo(tmp_path, base_images=LOCK_BASE_IMAGES)

    def test_fresh_lock_passes_and_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._prepare(monkeypatch, tmp_path)
        assert runner.invoke(lock.app, []).exit_code == 0
        before = (tmp_path / LOCK_PATH).read_text(encoding="utf-8")

        result = runner.invoke(lock.app, ["--check"])

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

        result = runner.invoke(lock.app, ["--check"])

        assert result.exit_code == 1, result.output
        assert "image lock is stale" in result.output
        entry = _read_lock(tmp_path)["images"]["UV_IMAGE"]
        assert entry["index"].endswith(f"uv:0.11.0@{DIGEST_B}")


def test_build_no_longer_carries_the_lock_flags() -> None:
    result = runner.invoke(build.app, ["--lock-only"])
    assert result.exit_code == 2

    result = runner.invoke(build.app, ["--check"])
    assert result.exit_code == 2

    result = runner.invoke(build.app, ["--upgrade"])
    assert result.exit_code == 2


def _selection_lock() -> lock_module.ImageLock:
    return lock_module.ImageLock.model_validate(
        {
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
        }
    )


def _selection_config(**sections: object) -> config_module.DockerDevkitConfig:
    payload: dict[str, object] = {"requires": ">=0"}
    payload.update(sections)
    return config_module.DockerDevkitConfig.model_validate(payload)


class TestEntrySelection:
    def test_entry_for_answers_the_index_without_a_platform(self) -> None:
        selected = lock_module.entry_for(_selection_lock(), "LOKI_IMAGE")

        assert selected.endswith(f"loki:3.5.0@{DIGEST_A}")

    def test_entry_for_answers_the_platform_manifest_for_a_named_platform(self) -> None:
        selected = lock_module.entry_for(_selection_lock(), "LOKI_IMAGE", "linux/arm64")

        assert selected.endswith(f"loki:3.5.0@{DIGEST_C}")

    def test_entry_for_answers_charts_flat_under_both_forms(self) -> None:
        lock = _selection_lock()

        assert lock_module.entry_for(lock, "GRAFANA_CHART") == lock_module.entry_for(
            lock, "GRAFANA_CHART", "linux/amd64"
        )

    def test_entry_for_refuses_a_platform_the_entry_never_recorded(self) -> None:
        with pytest.raises(RuntimeError, match=r"LOKI_IMAGE records no linux/ppc64le platform manifest.*run lock"):
            lock_module.entry_for(_selection_lock(), "LOKI_IMAGE", "linux/ppc64le")

    def test_entry_for_refuses_unknown_names(self) -> None:
        with pytest.raises(RuntimeError, match="lacks the GHOST_IMAGE entry"):
            lock_module.entry_for(_selection_lock(), "GHOST_IMAGE")


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

        with pytest.raises(RuntimeError, match=r"records image entries but docker-devkit\.yaml declares no platforms"):
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

        assert config.select_platform({"PLATFORM": ""}) == "linux/amd64"

    def test_a_multi_platform_repo_selects_the_named_platform(self) -> None:
        config = _selection_config(platforms=["linux/amd64", "linux/arm64"])

        assert config.select_platform({"PLATFORM": "linux/arm64"}) == "linux/arm64"

    def test_a_multi_platform_repo_without_a_selection_refuses_listing_declared(self) -> None:
        config = _selection_config(platforms=["linux/amd64", "linux/arm64"])

        with pytest.raises(RuntimeError, match=r"declares the platforms \['linux/amd64', 'linux/arm64'\].*PLATFORM="):
            config.select_platform({})

    def test_an_undeclared_selection_refuses_listing_declared(self) -> None:
        config = _selection_config(platforms=["linux/amd64"])

        with pytest.raises(RuntimeError, match=r"matches none of this repo's declared platforms \['linux/amd64'\]"):
            config.select_platform({"PLATFORM": "linux/arm64"})

    def test_a_platform_less_declaring_repo_answers_none(self) -> None:
        assert _selection_config().select_platform({}) is None
