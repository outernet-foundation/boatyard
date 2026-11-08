from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from boatyard.cli import app
from boatyard import crane
from boatyard.verbs import mirror
from boatyard.models.config_file import mirrored_ref
from boatyard.models.lock_file import LOCK_PATH

MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
FAKE_DIGEST = "sha256:" + "a" * 64
DIGEST_AMD64 = "sha256:" + "c" * 64
DIGEST_ARM64 = "sha256:" + "d" * 64

runner = CliRunner()


def _write_config(root: Path, sections: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"requires": ">=0.0", "project_name": "demo"}
    if sections is not None:
        payload.update(sections)
    payload.setdefault("services", {"ext": {"ref": "oci://ghcr.io/org/ext:0.1.0"}})
    (root / "boatyard.yml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _write_lock(
    root: Path,
    images: dict[str, object],
    charts: dict[str, str] | None = None,
    bundles: dict[str, str] | None = None,
    platforms: list[str] | None = None,
) -> None:
    platform_list = platforms if platforms is not None else ["linux/amd64"]
    entries: dict[str, object] = {}
    for name, value in images.items():
        entries[name] = (
            {"index": value, "platforms": dict.fromkeys(platform_list, value)} if isinstance(value, str) else value
        )
    document: dict[str, object] = {"platforms": platform_list, "images": entries}
    if charts:
        document["charts"] = charts
    if bundles:
        document["bundles"] = {name: {"reference": reference} for name, reference in bundles.items()}
    (root / LOCK_PATH).write_text(yaml.safe_dump(document), encoding="utf-8")


class CommandRecorder:
    def __init__(self, present_refs: set[str] | None = None) -> None:
        self.commands: list[str] = []
        self.present_refs = present_refs or set()

    def run(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("curl"):
            Path("go-containerregistry_Linux_x86_64.tar.gz").write_text("", encoding="utf-8")

    def check(self, command: str) -> bool:
        self.commands.append(command)
        return command.removeprefix("crane digest ") in self.present_refs


class TestMirroredRef:
    def test_prefixes_upstream_native_references(self):
        assert mirrored_ref("ghcr.io/astral-sh/uv:0.12.15", MIRROR_PREFIX) == (
            f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        )

    def test_rejects_already_prefixed_references(self):
        with pytest.raises(ValueError, match="upstream-native"):
            mirrored_ref(f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15", MIRROR_PREFIX)


class TestMirrorVerb:
    def _write_tree(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            {
                "mirror_prefix": MIRROR_PREFIX,
                "platforms": ["linux/amd64"],
                "base_images": {
                    "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
                    "API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
                },
            },
        )
        (tmp_path / "workloads").mkdir()
        _write_lock(
            tmp_path,
            {
                "UV_IMAGE": f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}",
                "API_IMAGE": f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{FAKE_DIGEST}",
            },
        )

    def _invoke(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: CommandRecorder) -> Result:
        monkeypatch.setattr(mirror, "bash", recorder.run)
        monkeypatch.setattr(crane, "bash", recorder.run)
        monkeypatch.setattr(mirror, "bash_check", recorder.check)
        monkeypatch.chdir(tmp_path)
        return runner.invoke(app, ["mirror"])

    def test_probes_every_recorded_digest_index_and_platforms(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        _write_config(
            tmp_path,
            {
                "mirror_prefix": MIRROR_PREFIX,
                "platforms": ["linux/amd64", "linux/arm64"],
                "base_images": {
                    "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
                    "API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
                },
            },
        )
        uv_base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        api_base = f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3"
        index = f"{uv_base}@{FAKE_DIGEST}"
        amd64 = f"{uv_base}@{DIGEST_AMD64}"
        arm64 = f"{uv_base}@{DIGEST_ARM64}"
        api_index = f"{api_base}@{FAKE_DIGEST}"
        api_amd64 = f"{api_base}@{DIGEST_AMD64}"
        api_arm64 = f"{api_base}@{DIGEST_ARM64}"
        _write_lock(
            tmp_path,
            {
                "UV_IMAGE": {"index": index, "platforms": {"linux/amd64": amd64, "linux/arm64": arm64}},
                "API_IMAGE": {"index": api_index, "platforms": {"linux/amd64": api_amd64, "linux/arm64": api_arm64}},
            },
            platforms=["linux/amd64", "linux/arm64"],
        )
        recorder = CommandRecorder(present_refs={index, amd64, arm64, api_index, api_amd64, api_arm64})

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        probes = [command.removeprefix("crane digest ") for command in recorder.commands if "crane digest" in command]
        assert probes == [index, amd64, arm64, api_index, api_amd64, api_arm64]
        assert not [command for command in recorder.commands if command.startswith("crane copy")]
        assert "Mirrored references: 0 copied, 2 already present" in result.stdout

    def test_a_missing_platform_manifest_triggers_the_copy_again(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        _write_config(
            tmp_path,
            {
                "mirror_prefix": MIRROR_PREFIX,
                "platforms": ["linux/amd64", "linux/arm64"],
                "base_images": {
                    "UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15",
                    "API_IMAGE": "ghcr.io/outernet-foundation/placeframe/api:1.2.3",
                },
            },
        )
        uv_base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15"
        api_base = f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3"
        index = f"{uv_base}@{FAKE_DIGEST}"
        amd64 = f"{uv_base}@{DIGEST_AMD64}"
        missing_arm64 = f"{uv_base}@{DIGEST_ARM64}"
        _write_lock(
            tmp_path,
            {
                "UV_IMAGE": {"index": index, "platforms": {"linux/amd64": amd64, "linux/arm64": missing_arm64}},
                "API_IMAGE": f"{api_base}@{FAKE_DIGEST}",
            },
            platforms=["linux/amd64", "linux/arm64"],
        )
        recorder = CommandRecorder(present_refs={index, amd64})

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        upstream_uv = "ghcr.io/astral-sh/uv:0.12.15"
        assert [command for command in recorder.commands if command.startswith("crane copy")] == [
            f"crane copy {upstream_uv} {MIRROR_PREFIX}/{upstream_uv}",
            f"crane copy ghcr.io/outernet-foundation/placeframe/api:1.2.3 {MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3",
        ]
        assert "Mirrored references: 2 copied, 0 already present" in result.stdout

    def test_probes_a_digest_once_when_index_and_platform_agree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        uv_base = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
        _write_lock(
            tmp_path,
            {
                "UV_IMAGE": {"index": uv_base, "platforms": {"linux/amd64": uv_base}},
                "API_IMAGE": f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{FAKE_DIGEST}",
            },
        )
        recorder = CommandRecorder(present_refs={uv_base})

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        assert recorder.commands.count(f"crane digest {uv_base}") == 1

    def test_copies_every_declared_reference(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = CommandRecorder()
        self._write_tree(tmp_path)

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        upstream_uv = "ghcr.io/astral-sh/uv:0.12.15"
        upstream_api = "ghcr.io/outernet-foundation/placeframe/api:1.2.3"
        assert [command for command in recorder.commands if command.startswith("crane copy")] == [
            f"crane copy {upstream_uv} {MIRROR_PREFIX}/{upstream_uv}",
            f"crane copy {upstream_api} {MIRROR_PREFIX}/{upstream_api}",
        ]
        assert "Mirrored references: 2 copied, 0 already present" in result.stdout

    def test_skips_references_the_lock_pins_as_mirrored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        mirrored_uv = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
        _write_lock(
            tmp_path,
            {
                "UV_IMAGE": mirrored_uv,
                "API_IMAGE": f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{FAKE_DIGEST}",
            },
        )
        recorder = CommandRecorder(present_refs={mirrored_uv})

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        upstream_api = "ghcr.io/outernet-foundation/placeframe/api:1.2.3"
        assert [command for command in recorder.commands if command.startswith("crane copy")] == [
            f"crane copy {upstream_api} {MIRROR_PREFIX}/{upstream_api}",
        ]
        assert "Mirrored references: 1 copied, 1 already present" in result.stdout

    def test_presence_probe_failure_falls_through_to_copy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_tree(tmp_path)
        mirrored_uv = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
        _write_lock(
            tmp_path,
            {
                "UV_IMAGE": mirrored_uv,
                "API_IMAGE": f"{MIRROR_PREFIX}/ghcr.io/outernet-foundation/placeframe/api:1.2.3@{FAKE_DIGEST}",
            },
        )
        recorder = CommandRecorder()

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        assert f"crane digest {mirrored_uv}" in recorder.commands
        assert len([command for command in recorder.commands if command.startswith("crane copy")]) == 2
        assert "Mirrored references: 2 copied, 0 already present" in result.stdout

    def test_copies_declared_charts_beside_images(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        upstream_chart = f"ghcr.io/grafana/helm-charts/loki@{FAKE_DIGEST}"
        recorder = CommandRecorder()
        monkeypatch.chdir(tmp_path)
        _write_config(
            tmp_path,
            {
                "platforms": ["linux/amd64"],
                "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
                "k8s": {"charts": {"loki": f"oci://{upstream_chart}"}},
            },
        )
        (tmp_path / "workloads").mkdir()
        _write_lock(
            tmp_path,
            {"UV_IMAGE": f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"},
            {"LOKI_CHART": f"{MIRROR_PREFIX}/{upstream_chart}"},
        )
        values = tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml"
        values.parent.mkdir(parents=True)
        values.write_text("loki: {}\n", encoding="utf-8")

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        assert f"crane copy ghcr.io/astral-sh/uv:0.12.15 {MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15" in (
            recorder.commands
        )
        assert f"crane copy {upstream_chart} {MIRROR_PREFIX}/{upstream_chart}" in recorder.commands
        assert "Mirrored references: 2 copied, 0 already present" in result.stdout

    def test_a_chart_without_its_lock_entry_refuses_naming_the_chart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        _write_config(
            tmp_path,
            {"k8s": {"charts": {"loki": f"oci://ghcr.io/grafana/helm-charts/loki@{FAKE_DIGEST}"}}},
        )
        (tmp_path / "workloads").mkdir()
        _write_lock(tmp_path, {}, platforms=[])
        values = tmp_path / "stack" / "score" / "k8s" / "values" / "loki.yaml"
        values.parent.mkdir(parents=True)
        values.write_text("loki: {}\n", encoding="utf-8")

        result = runner.invoke(app, ["mirror"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "LOKI_CHART" in str(result.exception)

    def test_never_enumerates_bundle_declarations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        bundle_ref = "oci://ghcr.io/org/repo/livekit:" + "a" * 40
        monkeypatch.chdir(tmp_path)
        _write_config(
            tmp_path,
            {
                "platforms": ["linux/amd64"],
                "base_images": {"UV_IMAGE": "ghcr.io/astral-sh/uv:0.12.15"},
                "project_name": "demo",
                "services": {"livekit": {"ref": bundle_ref}},
            },
        )
        _write_lock(
            tmp_path,
            {"UV_IMAGE": f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"},
            bundles={"LIVEKIT_BUNDLE": f"ghcr.io/org/repo/livekit:{'a' * 40}@{FAKE_DIGEST}"},
        )
        recorder = CommandRecorder(present_refs={f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"})

        result = self._invoke(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0
        assert not [command for command in recorder.commands if command.startswith("crane copy")]
        assert "Mirrored references: 0 copied, 1 already present" in result.stdout

    def test_errors_without_a_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["mirror"])

        assert result.exit_code != 0
        assert isinstance(result.exception, FileNotFoundError)

    def test_zero_declared_targets_is_a_loud_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path, {"requires": ">=0.1"})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["mirror"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "nothing is declared to mirror" in str(result.exception)

    def test_rejects_unpinned_declarations_loudly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path, {"base_images": {"ALPINE_IMAGE": "alpine"}})
        (tmp_path / "workloads").mkdir()
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["mirror"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
