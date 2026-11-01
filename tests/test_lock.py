from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from docker_devkit import config as config_module
from docker_devkit import lock as lock_module
from docker_devkit.config import LOCK_PATH
from docker_devkit.verbs import build, lock

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
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


def _write_repo(
    root: Path,
    *,
    base_images: dict[str, str] | None = None,
    charts: dict[str, str] | None = None,
) -> None:
    payload: dict[str, Any] = {"requires": ">=0"}
    if base_images:
        payload["platforms"] = ["linux/amd64"]
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
) -> Any:
    recorder = DigestRecorder()
    monkeypatch.setattr(lock_module, "resolve_remote_digest", recorder.resolve)
    monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
    monkeypatch.chdir(tmp_path)
    _write_repo(tmp_path, base_images=base_images, charts=charts)
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
    def test_records_declarations_under_the_mirror_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text("", encoding="utf-8")

        recorder = DigestRecorder()
        monkeypatch.setattr(lock_module, "resolve_remote_digest", recorder.resolve)
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
                "API_IMAGE": {"index": f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{DIGEST_A}"},
                "UV_IMAGE": {"index": f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}"},
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
        result = _invoke(
            monkeypatch, tmp_path, [], base_images={"ALPINE_IMAGE": f"docker.io/library/alpine@{DIGEST_A}"}
        )

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "images": {"ALPINE_IMAGE": {"index": f"{MIRROR_PREFIX}/docker.io/library/alpine@{DIGEST_A}"}}
        }

    def test_frozen_entries_resolve_again_only_on_upgrade(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {
                    "API_IMAGE": {
                        "index": f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{DIGEST_A}"
                    },
                    "UV_IMAGE": {"index": f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}"},
                }
            }),
            encoding="utf-8",
        )

        result = _invoke(monkeypatch, tmp_path, [], base_images=LOCK_BASE_IMAGES)
        assert result.exit_code == 0, result.output

        recorder = DigestRecorder()
        monkeypatch.setattr(lock_module, "resolve_remote_digest", recorder.resolve)
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
                    "API_IMAGE": {
                        "index": f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.2@{DIGEST_A}"
                    }
                }
            }),
            encoding="utf-8",
        )

        result = _invoke(
            monkeypatch, tmp_path, [], base_images={"API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3"}
        )

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path)["images"]["API_IMAGE"]["index"].endswith(f"api:1.2.3@{DIGEST_A}")

    def test_entries_whose_declarations_died_prune_by_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / LOCK_PATH).write_text(
            yaml.safe_dump({
                "images": {
                    "DEAD_OPERATOR_IMAGE": {"index": f"{MIRROR_PREFIX}/quay.io/op/operator:1.0@{DIGEST_B}"},
                    "RATHOLE_IMAGE": {"index": f"{MIRROR_PREFIX}/docker.io/x/rathole:0.11.0@{DIGEST_B}"},
                }
            }),
            encoding="utf-8",
        )

        result = _invoke(monkeypatch, tmp_path, [], base_images={"RATHOLE_IMAGE": "docker.io/x/rathole:0.11.0"})

        assert result.exit_code == 0, result.output
        assert _read_lock(tmp_path) == {
            "images": {"RATHOLE_IMAGE": {"index": f"{MIRROR_PREFIX}/docker.io/x/rathole:0.11.0@{DIGEST_B}"}}
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
            "images": {"UV_IMAGE": {"index": f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{DIGEST_A}"}}
        }


class TestLockCheck:
    def _prepare(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(config_module, "compute_service_shas", _no_service_shas)
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
                "images": {"UV_IMAGE": {"index": f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.11.0@{DIGEST_B}"}}
            }),
            encoding="utf-8",
        )

        result = runner.invoke(lock.app, ["--check"])

        assert result.exit_code == 1, result.output
        assert "image lock is stale" in result.output
        assert _read_lock(tmp_path)["images"]["UV_IMAGE"]["index"].endswith(f"uv:0.11.0@{DIGEST_B}")


def test_build_no_longer_carries_the_lock_flags() -> None:
    result = runner.invoke(build.app, ["--lock-only"])
    assert result.exit_code == 2

    result = runner.invoke(build.app, ["--check"])
    assert result.exit_code == 2

    result = runner.invoke(build.app, ["--upgrade"])
    assert result.exit_code == 2
