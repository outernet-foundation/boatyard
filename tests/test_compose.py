import platform
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from bashrun.bash import CalledProcessError, bash, bash_output
from typer.testing import CliRunner

from boatyard.cli import app
from boatyard import compose
from boatyard.verbs import down, up
from boatyard.config_lock import LOCK_PATH

MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
FAKE_DIGEST = "sha256:" + "a" * 64
PLATFORM_DIGEST = "sha256:" + "b" * 64
LIVEKIT_SHA_TAG = "a" * 40
LIVEKIT_REF = "oci://ghcr.io/org/livekit:" + LIVEKIT_SHA_TAG
LIVEKIT_PINNED = f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{FAKE_DIGEST}"
KEYCLOAK_REF = "oci://ghcr.io/org/keycloak:0.3.1-dev.42"
KEYCLOAK_PINNED = f"ghcr.io/org/keycloak:0.3.1-dev.42@{FAKE_DIGEST}"
TUNNEL_BUNDLES = {"TUNNEL_BUNDLE": LIVEKIT_PINNED}
PINNED_COMPOSE_VERSION = "5.6.1"

LOCKED_ERROR = f"Image {MIRROR_PREFIX}/docker.io/library/alpine:3.20 (digest {FAKE_DIGEST}) is not mirrored"

DEV_TOGGLES = "outernet.internal/dev-toggles"
OVERLAY_ARGUMENT_PATTERN = re.compile(r"-f (\S*devkit-dev-overlay-\S+\.yaml)")
ASSEMBLY_ARGUMENT_PATTERN = re.compile(r"-f (\S*devkit-assembly-\S+\.yaml)")
LOCK_ENV_ARGUMENT_PATTERN = re.compile(r"(\S*devkit-lock-env-\S+\.env)")

runner = CliRunner()


def _skip_crane_install() -> None:
    return None


def _patch_up_probes(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    monkeypatch.setattr(up, "install_crane", _skip_crane_install)
    monkeypatch.setattr(up, "bash_output", handler)


@pytest.fixture(scope="session")
def pinned_compose(tmp_path_factory: pytest.TempPathFactory) -> str:
    # Pin the exact compose binary the include-merge assertions run against.
    architecture = {"x86_64": "x86_64", "aarch64": "aarch64"}[platform.machine()]
    binary = tmp_path_factory.mktemp("compose-bin") / "docker-compose"
    release = (
        "https://github.com/docker/compose/releases/download/"
        f"v{PINNED_COMPOSE_VERSION}/docker-compose-linux-{architecture}"
    )
    bash(f"curl -fsSL {release} -o {binary}")
    binary.chmod(0o755)
    return str(binary)


def _write_config(root: Path, sections: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {"requires": ">=0.0"}
    if sections is not None:
        payload.update(sections)
    if "services" in payload and "gateway" not in payload:
        payload["gateway"] = "8080"
    (root / "boatyard.yml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _write_ref_tree(root: Path) -> None:
    # Write the minimal services-declaring repo: one bare remote include.
    _write_config(
        root,
        {
            "project_name": "demo",
            "platforms": ["linux/amd64"],
            "services": {"tunnel": {"ref": LIVEKIT_REF}},
        },
    )
    _write_lock(root, entries={}, bundles=TUNNEL_BUNDLES)


def _normalize(command: str, root: Path) -> str:
    command = re.sub(r"\S*devkit-assembly-\S+\.yaml", "ASSEMBLY", command)
    command = re.sub(r"\S*devkit-lock-env-\S+\.env", "LOCKENV", command)
    return command.replace(str(root), "ROOT")


def _write_lock(
    root: Path,
    entries: dict[str, str] | None = None,
    platform_entries: dict[str, str] | None = None,
    bundles: dict[str, str] | None = None,
) -> None:
    # Write one platform-manifest reference per declared platform beside the index.
    declared = (
        entries
        if entries is not None
        else {
            "CADDY_IMAGE": MIRROR_PREFIX + "/docker.io/library/caddy:2.8.4-alpine@" + FAKE_DIGEST,
        }
    )
    platforms = platform_entries or {"linux/amd64": ""}
    document: dict[str, object] = {
        "images": {
            name: {
                "index": reference,
                "platforms": {platform: manifest or reference for platform, manifest in platforms.items()},
            }
            for name, reference in declared.items()
        }
    }
    if bundles is not None:
        document["bundles"] = bundles
    root.joinpath(*LOCK_PATH.parts).write_text(
        yaml.safe_dump(document),
        encoding="utf-8",
    )


def _write_bundle_output(root: Path, name: str, services: list[str]) -> Path:
    path = root / "stack" / "generated" / "compose" / f"compose.{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"services": {service: {"image": f"demo/{service}:1"} for service in services}}),
        encoding="utf-8",
    )
    return path


def _write_workload(root: Path, name: str, service_name: str, annotations: dict[str, str] | None = None) -> None:
    path = root / "stack" / "score" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = {"name": service_name}
    if annotations is not None:
        metadata["annotations"] = annotations
    path.write_text(yaml.safe_dump({"metadata": metadata}), encoding="utf-8")


def _defined_profiles(command: str) -> str:
    assert "config --profiles" in command
    return "keycloak\ntunnel-ngrok\n"


def _rathole_profiles(command: str) -> str:
    assert "config --profiles" in command
    return "tunnel-rathole\n"


def _silent_handoff(_command: str) -> None:
    return None


def _probe_present(command: str) -> str:
    assert "imagetools inspect" in command or "crane digest" in command
    return ""


def _probe_absent(command: str) -> str:
    assert "imagetools inspect" in command or "crane digest" in command
    raise CalledProcessError(1, command, stderr="unexpected status from HEAD request: 404 Not Found\n")


def _probe_unauthorized(command: str) -> str:
    assert "imagetools inspect" in command or "crane digest" in command
    raise CalledProcessError(1, command, stderr="unauthorized: authentication required\n")


class SelectiveProbe:
    def __init__(self, present: str) -> None:
        self.present = present
        self.probed: list[str] = []

    def run(self, command: str) -> str:
        reference = command.rpartition("inspect ")[2]
        self.probed.append(reference)
        if reference == self.present:
            return ""
        raise CalledProcessError(1, command, stderr="unexpected status from HEAD request: 404 Not Found")


class HandoffRecorder:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str) -> None:
        self.commands.append(command)


class AssemblyRecorder(HandoffRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[dict[str, Any]] = []

    def run(self, command: str) -> None:
        super().run(command)
        match = ASSEMBLY_ARGUMENT_PATTERN.search(command)
        if match is not None:
            self.documents.append(yaml.safe_load(Path(match.group(1)).read_text(encoding="utf-8")))


class OverlayRecorder(HandoffRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[dict[str, Any]] = []

    def run(self, command: str) -> None:
        super().run(command)
        match = OVERLAY_ARGUMENT_PATTERN.search(command)
        if match is not None:
            self.documents.append(yaml.safe_load(Path(match.group(1)).read_text(encoding="utf-8")))


class LockEnvRecorder(HandoffRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.env_contents: list[str] = []

    def run(self, command: str) -> None:
        super().run(command)
        match = LOCK_ENV_ARGUMENT_PATTERN.search(command)
        if match is not None:
            self.env_contents.append(Path(match.group(1)).read_text(encoding="utf-8"))


class ServicesOutput:
    def __init__(self, *names: str) -> None:
        self.names = list(names)

    def run(self, command: str) -> str:
        assert "config --services" in command
        return "".join(f"{name}\n" for name in self.names)


class PresentProbeAndServices:
    def __init__(self, *names: str) -> None:
        self.names = list(names)

    def run(self, command: str) -> str:
        if "imagetools inspect" in command or "crane digest" in command:
            return ""
        assert "config --services" in command
        return "".join(f"{name}\n" for name in self.names)


class TestProfileForwarding:
    def _invoke_up(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> HandoffRecorder:
        _write_ref_tree(tmp_path)
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "bash_output", _defined_profiles)
        _patch_up_probes(monkeypatch, _probe_present)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)
        return recorder

    def test_forwards_profiles_declared_in_the_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=tunnel-ngrok\n", encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            (
                "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env "
                "--profile tunnel-ngrok up --pull missing -d"
            )
        ]

    def test_splits_commas_strips_whitespace_and_deduplicates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text('COMPOSE_PROFILES=" keycloak , , tunnel-ngrok ,keycloak"\n', encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            (
                "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env "
                "--profile keycloak --profile tunnel-ngrok up --pull missing -d"
            )
        ]

    def test_shell_environment_overrides_the_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=tunnel-ngrok\n", encoding="utf-8")
        monkeypatch.setenv("COMPOSE_PROFILES", "tunnel-rathole")
        recorder = HandoffRecorder()
        monkeypatch.setattr(compose, "bash_output", _rathole_profiles)
        _patch_up_probes(monkeypatch, _probe_present)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            (
                "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env "
                "--profile tunnel-rathole up --pull missing -d"
            )
        ]

    def test_empty_value_forwards_no_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env up --pull missing -d"
        ]

    def test_unknown_profile_fails_loudly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=keycloak,ghost\n", encoding="utf-8")
        self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "ghost" in str(result.exception)

    def test_absent_profiles_forward_no_flags(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        recorder = self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env up --pull missing -d"
        ]


def _write_consumer_tree(root: Path, *, bundles: str | None = "placeframe-cuda,auth-keycloak") -> Path:
    # Write the canonical consumer tree; bundles=None writes no BUNDLES line.
    _write_config(
        root,
        {
            "project_name": "demo",
            "repository": "ghcr.io/org/demo",
            "platforms": ["linux/amd64"],
            "gateway": "${GATEWAY_PORT:?err}",
            "services": {
                "auth": {
                    "options": {
                        "keycloak": {
                            "ref": KEYCLOAK_REF,
                            "routes": [{"path": "/auth/*", "upstream": "keycloak:8080", "strip_prefix": True}],
                        }
                    },
                },
                "placeframe": {
                    "options": {
                        "cuda": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "cuda"}},
                        "rocm": {"workloads": ["api.yaml"], "vars": {"GPU_VARIANT": "rocm"}},
                    },
                    "routes": [
                        {"path": "/grafana/*", "upstream": "grafana:3000"},
                        {"path": "/*", "upstream": "api:8000"},
                    ],
                },
                "livekit": {
                    "ref": LIVEKIT_REF,
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
    _write_lock(
        root,
        bundles={"AUTH_KEYCLOAK_BUNDLE": KEYCLOAK_PINNED, "LIVEKIT_BUNDLE": LIVEKIT_PINNED},
    )
    cuda = _write_bundle_output(root, "placeframe-cuda", ["api", "grafana"])
    _write_bundle_output(root, "placeframe-rocm", ["api", "grafana"])
    _write_workload(root, "api.yaml", "api")
    environment = "GATEWAY_PORT=58080\nLIVEKIT_SIGNALING_PORT=6178\nPUBLIC_URL=http://192.168.8.10:58080\n"
    if bundles is not None:
        environment = f"BUNDLES={bundles}\n" + environment
    (root / ".env").write_text(environment, encoding="utf-8")
    return cuda


class TestSelectionLaw:
    def _invoke_up(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        bundles: str | None = "placeframe-cuda,auth-keycloak",
        services: str = "api grafana edge livekit keycloak",
    ) -> AssemblyRecorder:
        _write_consumer_tree(tmp_path, bundles=bundles)
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        handler = PresentProbeAndServices(*services.split()).run
        monkeypatch.setattr(compose, "bash_output", handler)
        _patch_up_probes(monkeypatch, handler)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)
        return recorder

    def test_options_of_different_groups_select_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = self._invoke_up(tmp_path, monkeypatch)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == [
            "oci://" + KEYCLOAK_PINNED,
            (tmp_path / "stack/generated/compose/compose.placeframe-cuda.yaml").resolve().as_posix(),
            "oci://" + LIVEKIT_PINNED,
        ]

    def test_same_named_options_in_two_groups_select_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "platforms": ["linux/amd64"],
                "services": {
                    "one": {"options": {"cuda": {"workloads": ["api.yaml"]}}},
                    "two": {"options": {"cuda": {"workloads": ["edge.yaml"]}}},
                },
            },
        )
        _write_lock(tmp_path)
        one_cuda = _write_bundle_output(tmp_path, "one-cuda", ["api"])
        _write_bundle_output(tmp_path, "two-cuda", ["edge"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_workload(tmp_path, "edge.yaml", "edge")
        (tmp_path / ".env").write_text("BUNDLES=one-cuda,two-cuda\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        handler = PresentProbeAndServices("api", "edge").run
        monkeypatch.setattr(compose, "bash_output", handler)
        _patch_up_probes(monkeypatch, handler)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == [
            one_cuda.resolve().as_posix(),
            (tmp_path / "stack/generated/compose/compose.two-cuda.yaml").resolve().as_posix(),
        ]

    def test_zero_selection_is_legal_when_bare_entries_and_routes_carry_the_assembly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "gateway": "8080",
                "services": {
                    "livekit": {
                        "ref": LIVEKIT_REF,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                        "ports": [{"service": "livekit", "published": "6178", "target": "6178"}],
                    }
                },
            },
        )
        _write_lock(tmp_path, bundles={"LIVEKIT_BUNDLE": LIVEKIT_PINNED})
        (tmp_path / ".env").write_text("BUNDLES=\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        handler = PresentProbeAndServices("livekit", "edge").run
        monkeypatch.setattr(compose, "bash_output", handler)
        _patch_up_probes(monkeypatch, handler)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == ["oci://" + LIVEKIT_PINNED]

    def test_an_options_only_repo_refuses_a_selection_of_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "gateway": "8080",
                "services": {
                    "placeframe": {
                        "options": {
                            "cuda": {
                                "workloads": ["api.yaml"],
                                "routes": [{"path": "/*", "upstream": "api:8000"}],
                            }
                        }
                    }
                },
                "repository": "ghcr.io/org/demo",
            },
        )
        _write_lock(tmp_path)
        _write_bundle_output(tmp_path, "placeframe-cuda", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        (tmp_path / ".env").write_text("BUNDLES=\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        _patch_up_probes(monkeypatch, ServicesOutput("api", "edge").run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "selects nothing and no bare entry exists" in str(result.exception)

    def test_unknown_name_refuses_listing_the_vocabulary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch, bundles="placeframe-cuda,ghost")

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "ghost" in message
        assert "placeframe-cuda" in message and "auth-keycloak" in message and "livekit" in message

    def test_a_bare_entry_name_is_not_selectable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch, bundles="livekit")

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "options are" in str(result.exception)

    def test_two_options_of_one_group_refuse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch, bundles="placeframe-cuda,placeframe-rocm")

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "at most one option per group (placeframe)" in str(result.exception)

    def test_shell_environment_overrides_the_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch)
        monkeypatch.setenv("BUNDLES", "placeframe-rocm,auth-keycloak")

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output

    def test_stray_selection_in_an_undeclaring_repo_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("BUNDLES=cuda\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "declares no services" in str(result.exception)

    def test_up_without_config_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "must exist and declare services" in str(result.exception)

    def test_up_in_a_services_less_repo_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path)
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "must exist and declare services" in str(result.exception)

    def test_missing_local_output_refuses_naming_generate_score(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        (tmp_path / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml").unlink()
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "generate-score" in str(result.exception)


class TestUpMaterialization:
    def test_root_file_carries_name_includes_edge_and_port_stanzas(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path)
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        handler = PresentProbeAndServices("api", "grafana", "edge", "livekit", "keycloak").run
        monkeypatch.setattr(compose, "bash_output", handler)
        _patch_up_probes(monkeypatch, handler)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["name"] == "demo"
        assert document["include"] == [
            "oci://" + KEYCLOAK_PINNED,
            (tmp_path / "stack/generated/compose/compose.placeframe-cuda.yaml").resolve().as_posix(),
            "oci://" + LIVEKIT_PINNED,
        ]
        edge = document["services"]["edge"]
        assert edge["image"] == "${CADDY_IMAGE:?err}"
        assert edge["ports"] == [{"target": "8443", "published": "${GATEWAY_PORT:?err}", "protocol": "tcp"}]
        assert edge["environment"] == {
            "FORWARDED_PROTO": "http",
            "FORWARDED_PORT": "58080",
        }
        assert document["services"]["livekit"]["ports"] == [
            {
                "target": "${LIVEKIT_SIGNALING_PORT:?err}",
                "published": "${LIVEKIT_SIGNALING_PORT:?err}",
                "protocol": "tcp",
            }
        ]
        content = document["configs"]["devkit-edge-caddyfile"]["content"]
        assert "handle_path /auth/*" in content
        assert "header_up X-Forwarded-Proto {$$FORWARDED_PROTO}" in content
        assert "reverse_proxy grafana:3000" in content
        assert content.rstrip().endswith("\thandle {\n\t\treverse_proxy api:8000\n\t}\n}")

    def test_the_anonymous_edge_omits_the_auth_route_and_auth_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path, bundles="placeframe-cuda")
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        handler = PresentProbeAndServices("api", "grafana", "edge", "livekit").run
        monkeypatch.setattr(compose, "bash_output", handler)
        _patch_up_probes(monkeypatch, handler)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        edge = document["services"]["edge"]
        assert "environment" not in edge
        content = document["configs"]["devkit-edge-caddyfile"]["content"]
        assert "/auth/*" not in content

    def test_the_edge_refuses_without_the_caddy_lock_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        _write_lock(tmp_path, entries={"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST})
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "CADDY_IMAGE" in str(result.exception)

    def test_an_auth_selection_without_public_url_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        (tmp_path / ".env").write_text(
            "BUNDLES=placeframe-cuda,auth-keycloak\nGATEWAY_PORT=58080\nLIVEKIT_SIGNALING_PORT=6178\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "PUBLIC_URL" in str(result.exception)


class TestUpValidation:
    def _invoke_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services: str):
        _write_consumer_tree(tmp_path)
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.setattr(up, "bash_handoff", _silent_handoff)
        monkeypatch.setattr(compose, "bash_output", ServicesOutput(*services.split()).run)
        monkeypatch.chdir(tmp_path)
        return runner.invoke(app, ["up"])

    def test_a_route_upstream_absent_from_the_assembly_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result = self._invoke_up(tmp_path, monkeypatch, "api edge livekit keycloak")

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "/grafana/* upstream grafana" in str(result.exception)

    def test_the_selected_auth_upstream_absent_from_the_assembly_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result = self._invoke_up(tmp_path, monkeypatch, "api grafana edge livekit")

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "route /auth/* upstream keycloak" in str(result.exception)

    def test_a_port_owning_service_absent_from_the_assembly_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result = self._invoke_up(tmp_path, monkeypatch, "api grafana edge keycloak")

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "livekit" in str(result.exception)


class TestServiceExclusion:
    def test_two_selected_outputs_sharing_a_service_refuse_at_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "gateway": "8080",
                "repository": "ghcr.io/org/demo",
                "services": {
                    "left": {
                        "options": {
                            "a": {"workloads": ["api.yaml"], "routes": [{"path": "/*", "upstream": "api:8000"}]}
                        }
                    },
                    "right": {"options": {"b": {"workloads": ["api.yaml"]}}},
                },
            },
        )
        _write_lock(tmp_path)
        _write_bundle_output(tmp_path, "left-a", ["api"])
        _write_bundle_output(tmp_path, "right-b", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        (tmp_path / ".env").write_text("BUNDLES=left-a,right-b\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "compose.left-a.yaml" in message and "compose.right-b.yaml" in message
        assert "api" in message

    def test_the_same_overlap_stays_legal_at_down(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "gateway": "8080",
                "repository": "ghcr.io/org/demo",
                "services": {
                    "left": {
                        "options": {
                            "a": {"workloads": ["api.yaml"], "routes": [{"path": "/*", "upstream": "api:8000"}]}
                        }
                    },
                    "right": {"options": {"b": {"workloads": ["api.yaml"]}}},
                },
            },
        )
        _write_lock(tmp_path)
        _write_bundle_output(tmp_path, "left-a", ["api"])
        _write_bundle_output(tmp_path, "right-b", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        (tmp_path / ".env").write_text("BUNDLES=left-a,right-b\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(down, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert recorder.documents == [{"name": "demo"}]


class TestDownTeardown:
    def test_down_hands_off_a_name_only_root_with_remove_orphans(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(down, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert len(recorder.commands) == 1
        command = recorder.commands[0]
        assert command.startswith("docker compose -f ")
        assert f" --project-directory {tmp_path} --env-file .env --env-file " in command
        assert LOCK_ENV_ARGUMENT_PATTERN.search(command) is not None
        assert command.endswith("down --remove-orphans")
        match = ASSEMBLY_ARGUMENT_PATTERN.search(command)
        assert match is not None
        assert not Path(match.group(1)).exists()
        [document] = recorder.documents
        assert document == {"name": "demo"}

    def test_down_is_identical_under_every_selection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        commands: list[str] = []
        for bundles in ("placeframe-cuda,auth-keycloak", "", None):
            tree = tmp_path / f"tree-{len(commands)}"
            tree.mkdir()
            _write_consumer_tree(tree, bundles=bundles)
            monkeypatch.delenv("BUNDLES", raising=False)
            recorder = AssemblyRecorder()
            monkeypatch.setattr(down, "bash_handoff", recorder.run)
            monkeypatch.chdir(tree)

            result = runner.invoke(app, ["down"])

            assert result.exit_code == 0, result.output
            normalized = _normalize(recorder.commands[0], tree)
            commands.append(normalized)
        assert commands[0] == commands[1] == commands[2]

    def test_down_forwards_the_volumes_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.setattr(down, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down", "--volumes"])

        assert result.exit_code == 0, result.output
        assert recorder.commands[0].endswith("down --remove-orphans -v")

    def test_down_without_config_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "must exist and declare services" in str(result.exception)

    def test_down_in_a_services_less_repo_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path)
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "must exist and declare services" in str(result.exception)


def _write_dev_tree(
    root: Path,
    *,
    dockerfile: str,
    api_annotations: dict[str, str] | None = None,
    edge_annotations: dict[str, str] | None = None,
) -> None:
    _write_config(
        root,
        {
            "project_name": "demo",
            "repository": "ghcr.io/org/demo",
            "services": {
                "apps": {
                    "options": {
                        "core": {"workloads": ["api.yaml"]},
                        "edge": {"workloads": ["edge.yaml"]},
                    }
                }
            },
        },
    )
    service_directory = root / "workloads" / "api"
    service_directory.mkdir(parents=True, exist_ok=True)
    (service_directory / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    _write_bundle_output(root, "apps-core", ["api"])
    _write_bundle_output(root, "apps-edge", ["edge"])
    _write_workload(root, "api.yaml", "api", api_annotations)
    _write_workload(root, "edge.yaml", "edge", edge_annotations)
    (root / ".env").write_text("BUNDLES=apps-core\n", encoding="utf-8")


MULTI_STAGE_DOCKERFILE = "ARG BASE_IMAGE\nFROM ${BASE_IMAGE} AS dev\nFROM dev AS runtime\n"
SINGLE_STAGE_DOCKERFILE = "ARG BASE_IMAGE\nFROM ${BASE_IMAGE} AS dev\n"


class TestDevOverlay:
    def test_dev_stage_service_gets_dev_image_repo_bind_and_toggle_holes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_dev_tree(
            tmp_path,
            dockerfile=MULTI_STAGE_DOCKERFILE,
            api_annotations={DEV_TOGGLES: "RELOAD=true,DEBUG_API=false"},
        )
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        recorder = OverlayRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up", "--dev"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document == {
            "services": {
                "api": {
                    "image": "ghcr.io/org/demo/api:dev",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": tmp_path.resolve().as_posix(),
                            "target": "/app",
                            "read_only": True,
                        }
                    ],
                    "environment": {"RELOAD": "${RELOAD:-true}", "DEBUG_API": "${DEBUG_API:-false}"},
                }
            }
        }

    def test_toggle_only_service_gets_environment_holes_without_an_image(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_dev_tree(tmp_path, dockerfile=SINGLE_STAGE_DOCKERFILE, api_annotations={DEV_TOGGLES: "RELOAD=true"})
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        recorder = OverlayRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up", "--dev"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document == {"services": {"api": {"environment": {"RELOAD": "${RELOAD:-true}"}}}}

    def test_overlay_targets_only_services_the_selection_defines(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_dev_tree(
            tmp_path,
            dockerfile=MULTI_STAGE_DOCKERFILE,
            api_annotations={DEV_TOGGLES: "RELOAD=true"},
            edge_annotations={DEV_TOGGLES: "DEBUG_EDGE=false"},
        )
        (tmp_path / ".env").write_text("BUNDLES=apps-edge\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        recorder = OverlayRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up", "--dev"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document == {"services": {"edge": {"environment": {"DEBUG_EDGE": "${DEBUG_EDGE:-false}"}}}}

    def test_up_dev_without_dev_shape_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_dev_tree(tmp_path, dockerfile=SINGLE_STAGE_DOCKERFILE)
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up", "--dev"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "requires a dev shape" in str(result.exception)

    def test_up_dev_builds_both_shapes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_dev_tree(tmp_path, dockerfile=MULTI_STAGE_DOCKERFILE)
        monkeypatch.delenv("BUNDLES", raising=False)
        builds: list[dict[str, object]] = []

        def _record_build(**kwargs: object) -> None:
            builds.append(dict(kwargs))

        monkeypatch.setattr(up, "run_build", _record_build)
        monkeypatch.setattr(up, "bash_handoff", _silent_handoff)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up", "--build", "--dev"])

        assert result.exit_code == 0, result.output
        assert [set(build) for build in builds] == [{"config"}, {"config", "dev"}]
        assert builds[0]["config"] is builds[1]["config"]

    def test_up_dev_layers_and_cleans_the_overlay(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_dev_tree(
            tmp_path,
            dockerfile=MULTI_STAGE_DOCKERFILE,
            api_annotations={DEV_TOGGLES: "RELOAD=true"},
        )
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up", "--dev"])

        assert result.exit_code == 0, result.output
        overlay_match = OVERLAY_ARGUMENT_PATTERN.search(recorder.commands[0])
        assert overlay_match is not None
        assert not Path(overlay_match.group(1)).exists()


class TestLockRequirement:
    def test_a_bundles_only_lock_attaches_no_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(down, "bash_handoff", recorder.run)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env down --remove-orphans"
        ]

    def test_present_lock_attaches_the_second_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("PUBLIC_URL=https://example.dev\n", encoding="utf-8")
        _write_lock(tmp_path, {"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST})
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(down, "bash_handoff", recorder.run)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env --env-file LOCKENV down --remove-orphans"
        ]

    def test_a_repo_without_a_lock_skips_the_probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        (tmp_path / ".env").write_text("BUNDLES=apps-core\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env up --pull missing -d"
        ]


class TestLockEnvSynthesis:
    ALPINE_INDEX = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST
    ALPINE_MANIFEST = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + PLATFORM_DIGEST

    def _invoke_up_with_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: HandoffRecorder):
        monkeypatch.delenv("PLATFORM", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, _probe_present)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)
        return runner.invoke(app, ["up"])

    def test_the_attached_env_file_carries_the_platform_manifest_not_the_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        _write_lock(
            tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST}, TUNNEL_BUNDLES
        )
        recorder = LockEnvRecorder()

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0, result.output
        [content] = recorder.env_contents
        assert content == f"ALPINE_IMAGE={self.ALPINE_MANIFEST}\n"

    def test_two_consecutive_syntheses_are_byte_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        _write_lock(
            tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST}, TUNNEL_BUNDLES
        )
        recorder = LockEnvRecorder()

        self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)
        self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert len(recorder.env_contents) == 2
        assert recorder.env_contents[0] == recorder.env_contents[1]

    def test_a_multi_platform_repo_selects_the_named_platform(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64", "linux/arm64"],
                "services": {"tunnel": {"ref": LIVEKIT_REF}},
            },
        )
        (tmp_path / ".env").write_text("PLATFORM=linux/arm64\n", encoding="utf-8")
        arm64_manifest = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + PLATFORM_DIGEST
        _write_lock(
            tmp_path,
            {"ALPINE_IMAGE": self.ALPINE_INDEX},
            {"linux/amd64": self.ALPINE_MANIFEST, "linux/arm64": arm64_manifest},
            TUNNEL_BUNDLES,
        )
        recorder = LockEnvRecorder()

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0, result.output
        assert recorder.env_contents[0] == f"ALPINE_IMAGE={arm64_manifest}\n"

    def test_a_multi_platform_repo_without_a_selection_refuses_listing_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64", "linux/arm64"],
                "services": {"tunnel": {"ref": LIVEKIT_REF}},
            },
        )
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, bundles=TUNNEL_BUNDLES)

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, HandoffRecorder())

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "PLATFORM=" in message
        assert "linux/amd64" in message and "linux/arm64" in message

    def test_an_undeclared_platform_selection_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("PLATFORM=linux/ppc64le\n", encoding="utf-8")
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, bundles=TUNNEL_BUNDLES)

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, HandoffRecorder())

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "linux/ppc64le" in message
        assert "linux/amd64" in message

    def test_an_entry_lacking_the_selected_platform_manifest_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        root_lock = tmp_path.joinpath(*LOCK_PATH.parts)
        root_lock.write_text(
            yaml.safe_dump({"images": {"ALPINE_IMAGE": {"index": self.ALPINE_INDEX}}, "bundles": TUNNEL_BUNDLES}),
            encoding="utf-8",
        )

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, HandoffRecorder())

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "ALPINE_IMAGE" in message
        assert "linux/amd64" in message
        assert "platform manifest" in message

    def test_a_failed_run_prints_and_keeps_the_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        _write_lock(
            tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST}, TUNNEL_BUNDLES
        )
        monkeypatch.delenv("PLATFORM", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, _probe_absent)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        kept = [
            Path(line.split(":", 1)[1].strip()) for line in result.output.splitlines() if "kept for inspection" in line
        ]
        assert len(kept) == 1
        assert kept[0].read_text(encoding="utf-8") == f"ALPINE_IMAGE={self.ALPINE_MANIFEST}\n"


class TestMirroredLockEnforcement:
    def _write_mirrored_tree(self, tmp_path: Path, entries: dict[str, str]) -> None:
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        _write_lock(tmp_path, entries, bundles=TUNNEL_BUNDLES)

    def test_missing_entry_fails_closed_with_the_locked_message(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_mirrored_tree(
            tmp_path, {"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST}
        )
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, _probe_absent)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert str(result.exception) == LOCKED_ERROR

    def test_probe_failure_is_not_reported_as_an_unmirrored_image(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._write_mirrored_tree(
            tmp_path, {"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST}
        )
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, _probe_unauthorized)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        message = str(result.exception)
        assert "not mirrored" not in message
        assert "the mirror probe failed" in message
        assert "authentication required" in message
        assert "alpine" in message

    def test_present_entries_proceed_to_bring_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._write_mirrored_tree(
            tmp_path, {"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST}
        )
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        recorder = HandoffRecorder()
        _patch_up_probes(monkeypatch, _probe_present)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env --env-file LOCKENV up --pull missing -d"
        ]

    def test_reports_the_first_missing_entry_deterministically(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        present = f"{MIRROR_PREFIX}/ghcr.io/astral-sh/uv:0.12.15@{FAKE_DIGEST}"
        absent = f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{FAKE_DIGEST}"
        probe = SelectiveProbe(present)
        self._write_mirrored_tree(tmp_path, {"UV_IMAGE": present, "ALPINE_IMAGE": absent})
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, probe.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert "alpine" in str(result.exception)
        assert probe.probed == [absent]

    def test_a_missing_platform_manifest_digest_fails_closed_naming_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        index = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST
        manifest = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + PLATFORM_DIGEST
        self._write_mirrored_tree(tmp_path, {"ALPINE_IMAGE": index})
        tmp_path.joinpath(*LOCK_PATH.parts).write_text(
            yaml.safe_dump({
                "images": {
                    "ALPINE_IMAGE": {"index": index, "platforms": {"linux/amd64": manifest}},
                },
                "bundles": TUNNEL_BUNDLES,
            }),
            encoding="utf-8",
        )
        probe = SelectiveProbe(index)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, probe.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert str(result.exception) == (
            f"Image {MIRROR_PREFIX}/docker.io/library/alpine:3.20 (digest {PLATFORM_DIGEST}) is not mirrored"
        )
        assert probe.probed == [index, manifest]

    def test_a_probe_failure_over_a_platform_manifest_is_not_an_unmirrored_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        index = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST
        manifest = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + PLATFORM_DIGEST
        self._write_mirrored_tree(tmp_path, {"ALPINE_IMAGE": index})
        tmp_path.joinpath(*LOCK_PATH.parts).write_text(
            yaml.safe_dump({
                "images": {
                    "ALPINE_IMAGE": {"index": index, "platforms": {"linux/amd64": manifest}},
                },
                "bundles": TUNNEL_BUNDLES,
            }),
            encoding="utf-8",
        )
        probed: list[str] = []

        def _probe_then_fail(command: str) -> str:
            reference = command.rpartition("inspect ")[2]
            probed.append(reference)
            if reference == index:
                return ""
            raise CalledProcessError(1, command, stderr="unauthorized: authentication required\n")

        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, _probe_then_fail)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        message = str(result.exception)
        assert "not mirrored" not in message
        assert "the mirror probe failed" in message
        assert PLATFORM_DIGEST in message
        assert probed == [index, manifest]


class TestBundleLockEnforcement:
    def test_a_selected_ref_without_its_bundle_lock_entry_refuses_naming_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        _write_lock(tmp_path, entries={}, bundles={})
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "TUNNEL_BUNDLE" in message
        assert "run boatyard lock" in message

    def test_the_bundle_include_carries_the_locks_digest_not_the_yaml_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        relocked = dict(TUNNEL_BUNDLES)
        relocked["TUNNEL_BUNDLE"] = f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{PLATFORM_DIGEST}"
        _write_lock(tmp_path, entries={}, bundles=relocked)
        recorder = AssemblyRecorder()
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, _probe_present)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == ["oci://" + relocked["TUNNEL_BUNDLE"]]

    def test_the_bundle_probe_uses_crane_over_the_recorded_digest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        probed: list[str] = []

        def _record(command: str) -> str:
            assert "crane digest" in command
            probed.append(command.removeprefix("crane digest "))
            return ""

        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, _record)
        monkeypatch.setattr(up, "bash_handoff", _silent_handoff)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert probed == [LIVEKIT_PINNED]

    def test_an_unpublished_bundle_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, _probe_absent)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert str(result.exception) == (
            f"Bundle ghcr.io/org/livekit:{LIVEKIT_SHA_TAG} (digest {FAKE_DIGEST}) is not published; run boatyard lock"
        )

    def test_a_bundle_probe_failure_is_not_an_unpublished_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
        _patch_up_probes(monkeypatch, _probe_unauthorized)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        message = str(result.exception)
        assert "not published" not in message
        assert "the bundle probe failed" in message
        assert "authentication required" in message


class TestComposePortMergeOverIncludes:
    def test_parent_ports_add_to_and_reset_included_ports(self, tmp_path: Path, pinned_compose: str) -> None:
        child = tmp_path / "child.yaml"
        child.write_text(
            'services:\n  svc:\n    image: docker.io/library/alpine:3.20\n    ports:\n      - "18443:8443"\n',
            encoding="utf-8",
        )
        parent = tmp_path / "parent.yaml"
        parent.write_text(
            'include:\n  - ./child.yaml\nservices:\n  svc:\n    ports:\n      - "58080:8443"\n',
            encoding="utf-8",
        )

        rendered = bash_output(f"{pinned_compose} -f {parent} --project-directory {tmp_path} config --format json")
        document = yaml.safe_load(rendered)
        ports = document["services"]["svc"]["ports"]
        assert {port["published"] for port in ports} == {"18443", "58080"}

        reset_parent = tmp_path / "reset-parent.yaml"
        reset_parent.write_text(
            "include:\n  - ./child.yaml\nservices:\n  svc:\n    ports: !reset []\n",
            encoding="utf-8",
        )
        rendered = bash_output(
            f"{pinned_compose} -f {reset_parent} --project-directory {tmp_path} config --format json"
        )
        document = yaml.safe_load(rendered)
        assert "ports" not in document["services"]["svc"]
