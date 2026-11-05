import platform
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from bashrun.bash import bash, bash_output
from typer.testing import CliRunner

from boatyard.cli import app
from boatyard.verbs import down, up
from boatyard.lock_file import LOCK_PATH

MIRROR_PREFIX = "ghcr.io/outernet-foundation/mirror"
FAKE_DIGEST = "sha256:" + "a" * 64
PLATFORM_DIGEST = "sha256:" + "b" * 64
LOKI_AMD64_DIGEST = "sha256:" + "c" * 64
LOKI_ARM64_DIGEST = "sha256:" + "d" * 64
CONSUMER_ALPINE_DIGEST = "sha256:" + "e" * 64
PRODUCER_ALPINE_DIGEST = "sha256:" + "f" * 64
OTHER_SHA_TAG = "b" * 40
LIVEKIT_SHA_TAG = "a" * 40
LIVEKIT_REF = "oci://ghcr.io/org/livekit:" + LIVEKIT_SHA_TAG
LIVEKIT_PINNED = f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{FAKE_DIGEST}"
KEYCLOAK_REF = "oci://ghcr.io/org/keycloak:0.3.1-dev.42"
KEYCLOAK_PINNED = f"ghcr.io/org/keycloak:0.3.1-dev.42@{FAKE_DIGEST}"
PLACEFRAME_CUDA_REF = "oci://ghcr.io/org/placeframe:1.2.3"
PLACEFRAME_ROCM_REF = "oci://ghcr.io/org/placeframe:1.2.3-rocm"
TUNNEL_BUNDLES = {"TUNNEL_BUNDLE": LIVEKIT_PINNED}
PINNED_COMPOSE_VERSION = "5.6.1"

ASSEMBLY_ARGUMENT_PATTERN = re.compile(r"-f (\S*devkit-assembly-\S+\.yaml)")
LOCK_ENV_ARGUMENT_PATTERN = re.compile(r"(\S*devkit-lock-env-\S+\.env)")

runner = CliRunner()


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
        document["bundles"] = {name: {"reference": reference} for name, reference in bundles.items()}
    root.joinpath(*LOCK_PATH.parts).write_text(
        yaml.safe_dump(document),
        encoding="utf-8",
    )


def _write_bundle_output(root: Path, name: str, services: list[str]) -> Path:
    path = root / "stack" / "generated" / "compose" / f"compose.{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    network = f"{name}-net"
    path.write_text(
        yaml.safe_dump({
            "services": {
                f"{name}-{service}": {
                    "image": f"demo/{service}:1",
                    "networks": {network: {"aliases": [service]}},
                }
                for service in services
            },
            "networks": {network: None},
        }),
        encoding="utf-8",
    )
    return path


def _write_producer(root: Path, services: list[str], lock: dict[str, object] | None = None) -> Path:
    # A sibling producer checkout carrying the fixed stack/generated/compose/ output layout.
    output = root / "producer" / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml"
    output.parent.mkdir(parents=True, exist_ok=True)
    network = "placeframe-cuda-net"
    output.write_text(
        yaml.safe_dump({
            "services": {
                f"placeframe-cuda-{service}": {
                    "image": f"demo/{service}:1",
                    "networks": {network: {"aliases": [service]}},
                }
                for service in services
            },
            "networks": {network: None},
        }),
        encoding="utf-8",
    )
    if lock is not None:
        (root / "producer").joinpath(*LOCK_PATH.parts).write_text(yaml.safe_dump(lock), encoding="utf-8")
    return output


def _producer_lock(platforms: list[str]) -> dict[str, object]:
    loki_manifests = {
        platform: f"{MIRROR_PREFIX}/docker.io/library/loki:3.0@{digest}"
        for platform, digest in (("linux/amd64", LOKI_AMD64_DIGEST), ("linux/arm64", LOKI_ARM64_DIGEST))
        if platform in platforms
    }
    alpine_manifests = dict.fromkeys(
        platforms, f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}"
    )
    return {
        "images": {
            "LOKI_IMAGE": {
                "index": f"{MIRROR_PREFIX}/docker.io/library/loki:3.0@{FAKE_DIGEST}",
                "platforms": loki_manifests,
            },
            "ALPINE_IMAGE": {
                "index": f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}",
                "platforms": alpine_manifests,
            },
        }
    }


def _write_override_consumer(
    root: Path,
    environment_lines: list[str],
    *,
    platforms: list[str] | None = None,
    lock_images: dict[str, str] | None = None,
    services_extra: dict[str, object] | None = None,
) -> None:
    # The overridden placeframe-cuda ref gets no lock entry; the override replaces its registry source.
    services: dict[str, object] = {
        "placeframe": {
            "options": {
                "cuda": {"ref": PLACEFRAME_CUDA_REF},
                "rocm": {"ref": PLACEFRAME_ROCM_REF},
            }
        },
    }
    if services_extra is not None:
        services.update(services_extra)
    payload: dict[str, object] = {
        "project_name": "demo",
        "repository": "ghcr.io/org/demo",
        "services": services,
    }
    if platforms is not None:
        payload["platforms"] = platforms
    _write_config(root, payload)
    _write_lock(root, entries=dict(lock_images) if lock_images is not None else {})
    (root / ".env").write_text(
        "BUNDLES=placeframe-cuda\n" + "".join(f"{line}\n" for line in environment_lines),
        encoding="utf-8",
    )


def _write_workload(root: Path, name: str, service_name: str) -> None:
    path = root / "stack" / "score" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"metadata": {"name": service_name}}), encoding="utf-8")


def _silent_handoff(_command: str) -> None:
    return None


def _failing_handoff(_command: str) -> None:
    raise RuntimeError("compose failed")


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


class LockEnvRecorder(HandoffRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.env_contents: list[str] = []

    def run(self, command: str) -> None:
        super().run(command)
        match = LOCK_ENV_ARGUMENT_PATTERN.search(command)
        if match is not None:
            self.env_contents.append(Path(match.group(1)).read_text(encoding="utf-8"))


class ProducerEnvRecorder(AssemblyRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.env_contents: list[str] = []

    def run(self, command: str) -> None:
        super().run(command)
        match = LOCK_ENV_ARGUMENT_PATTERN.search(command)
        if match is not None:
            self.env_contents.append(Path(match.group(1)).read_text(encoding="utf-8"))


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
    ) -> AssemblyRecorder:
        _write_consumer_tree(tmp_path, bundles=bundles)
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)
        return recorder

    def test_options_of_different_groups_select_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = self._invoke_up(tmp_path, monkeypatch)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert sorted(document["include"]) == sorted([
            "oci://" + KEYCLOAK_PINNED,
            (tmp_path / "stack/generated/compose/compose.placeframe-cuda.yaml").resolve().as_posix(),
            "oci://" + LIVEKIT_PINNED,
        ])

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
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == ["oci://" + LIVEKIT_PINNED]

    def test_an_options_only_repo_with_an_empty_selection_hands_compose_a_name_only_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
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
        recorder = AssemblyRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert recorder.documents == [{"name": "demo"}]

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
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["name"] == "demo"
        assert sorted(document["include"]) == sorted([
            "oci://" + KEYCLOAK_PINNED,
            (tmp_path / "stack/generated/compose/compose.placeframe-cuda.yaml").resolve().as_posix(),
            "oci://" + LIVEKIT_PINNED,
        ])
        edge = document["services"]["edge"]
        assert edge["image"] == "${CADDY_IMAGE:?err}"
        assert edge["ports"] == [{"target": "8443", "published": "${GATEWAY_PORT:?err}", "protocol": "tcp"}]
        assert edge["networks"] == {
            "auth-keycloak-net": None,
            "placeframe-cuda-net": None,
            "livekit-net": None,
        }
        assert edge["environment"] == {
            "FORWARDED_PROTO": "http",
            "FORWARDED_PORT": "58080",
        }
        assert document["services"]["livekit-livekit"]["ports"] == [
            {
                "target": "${LIVEKIT_SIGNALING_PORT:?err}",
                "published": "${LIVEKIT_SIGNALING_PORT:?err}",
                "protocol": "tcp",
            }
        ]
        content = document["configs"]["devkit-edge-caddyfile"]["content"]
        assert "handle_path /auth/*" in content
        assert "header_up X-Forwarded-Proto {$$FORWARDED_PROTO}" in content
        assert "reverse_proxy placeframe-cuda-grafana:3000" in content
        assert content.rstrip().endswith("\thandle {\n\t\treverse_proxy placeframe-cuda-api:8000\n\t}\n}")

    def test_the_anonymous_edge_omits_the_auth_route_and_auth_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path, bundles="placeframe-cuda")
        monkeypatch.delenv("BUNDLES", raising=False)
        recorder = AssemblyRecorder()
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
    def _invoke_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.setattr(up, "bash_handoff", _silent_handoff)
        monkeypatch.chdir(tmp_path)
        return runner.invoke(app, ["up"])

    def test_a_local_route_upstream_absent_from_the_output_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path)
        _write_bundle_output(tmp_path, "placeframe-cuda", ["api"])

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "services entry 'placeframe-cuda'" in message
        assert "'/grafana/*' references 'placeframe-cuda-grafana'" in message

    def test_a_local_port_owner_absent_from_the_output_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_bundle_output(tmp_path, "placeframe-cuda", ["api"])
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "platforms": ["linux/amd64"],
                "gateway": "8080",
                "services": {
                    "placeframe": {
                        "options": {
                            "cuda": {
                                "workloads": ["api.yaml"],
                                "ports": [{"service": "grafana", "published": "3306", "target": "3306"}],
                            }
                        }
                    }
                },
            },
        )
        _write_workload(tmp_path, "api.yaml", "api")
        _write_lock(tmp_path, entries={})
        (tmp_path / ".env").write_text("BUNDLES=placeframe-cuda\n", encoding="utf-8")

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "'3306' references 'placeframe-cuda-grafana'" in str(result.exception)

    def test_a_local_output_hole_no_environment_source_fills_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path)
        cuda = tmp_path / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml"
        cuda.write_text(
            "services:\n"
            "  placeframe-cuda-api:\n"
            "    image: demo/api:1\n"
            "    environment:\n"
            "      TOKEN: ${API_TOKEN}\n"
            "    networks:\n"
            "      placeframe-cuda-net:\n"
            "        aliases:\n"
            "          - api\n"
            "  placeframe-cuda-grafana:\n"
            "    image: demo/grafana:1\n"
            "    networks:\n"
            "      placeframe-cuda-net:\n"
            "        aliases:\n"
            "          - grafana\n"
            "networks:\n"
            "  placeframe-cuda-net: null\n",
            encoding="utf-8",
        )

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "bundle 'placeframe-cuda' requires ['API_TOKEN']" in str(result.exception)

    def test_a_lock_recorded_requirement_no_environment_source_fills_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path)
        root_lock = tmp_path.joinpath(*LOCK_PATH.parts)
        root_lock.write_text(
            root_lock.read_text(encoding="utf-8").replace(
                f"  LIVEKIT_BUNDLE:\n    reference: {LIVEKIT_PINNED}",
                f"  LIVEKIT_BUNDLE:\n    reference: {LIVEKIT_PINNED}\n    requires:\n      - LIVEKIT_TOKEN",
            ),
            encoding="utf-8",
        )

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "bundle 'livekit' requires ['LIVEKIT_TOKEN']" in str(result.exception)

    def test_a_requirement_filled_by_the_env_file_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        root_lock = tmp_path.joinpath(*LOCK_PATH.parts)
        root_lock.write_text(
            root_lock.read_text(encoding="utf-8").replace(
                f"  LIVEKIT_BUNDLE:\n    reference: {LIVEKIT_PINNED}",
                f"  LIVEKIT_BUNDLE:\n    reference: {LIVEKIT_PINNED}\n    requires:\n      - LIVEKIT_TOKEN",
            ),
            encoding="utf-8",
        )
        (tmp_path / ".env").write_text(
            (tmp_path / ".env").read_text(encoding="utf-8") + "LIVEKIT_TOKEN=set\n",
            encoding="utf-8",
        )

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output


class TestSharedNames:
    def test_two_selected_outputs_sharing_a_service_name_namespace_apart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
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
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert sorted(document["include"]) == sorted([
            (tmp_path / "stack/generated/compose/compose.left-a.yaml").resolve().as_posix(),
            (tmp_path / "stack/generated/compose/compose.right-b.yaml").resolve().as_posix(),
        ])
        content = document["configs"]["devkit-edge-caddyfile"]["content"]
        assert "reverse_proxy left-a-api:8000" in content

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

    def test_down_refuses_an_unknown_selection_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("BUNDLES=ghost\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "the selectable options are" in str(result.exception)

    def test_down_refuses_two_options_from_one_group(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "services": {
                    "tunnel": {
                        "options": {
                            "ngrok": {"ref": "oci://ghcr.io/org/ngrok:0.1.0"},
                            "rathole": {"ref": "oci://ghcr.io/org/rathole:0.1.0"},
                        }
                    }
                },
            },
        )
        _write_lock(
            tmp_path,
            bundles={
                "TUNNEL_NGROK_BUNDLE": "ghcr.io/org/ngrok:0.1.0@" + FAKE_DIGEST,
                "TUNNEL_RATHOLE_BUNDLE": "ghcr.io/org/rathole:0.1.0@" + FAKE_DIGEST,
            },
        )
        (tmp_path / ".env").write_text("BUNDLES=tunnel-ngrok,tunnel-rathole\n", encoding="utf-8")
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "at most one option per group (tunnel)" in str(result.exception)


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
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)
        return runner.invoke(app, ["up"])

    def test_the_attached_env_file_carries_the_platform_manifest_not_the_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("", encoding="utf-8")
        _write_lock(
            tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST}, TUNNEL_BUNDLES
        )
        recorder = LockEnvRecorder()

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0, result.output
        [content] = recorder.env_contents
        assert content == f"ALPINE_IMAGE={self.ALPINE_MANIFEST}\n"

    def test_a_selected_local_entry_reads_its_lock_keys_under_the_entry_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "platforms": ["linux/amd64"],
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST})
        (tmp_path / ".env").write_text("BUNDLES=apps-core\n", encoding="utf-8")
        recorder = LockEnvRecorder()

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0, result.output
        [content] = recorder.env_contents
        assert content == f"ALPINE_IMAGE={self.ALPINE_MANIFEST}\nAPPS_CORE_ALPINE_IMAGE={self.ALPINE_MANIFEST}\n"

    def test_two_consecutive_syntheses_are_byte_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("", encoding="utf-8")
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
        (tmp_path / ".env").write_text("", encoding="utf-8")
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
        (tmp_path / ".env").write_text("", encoding="utf-8")
        root_lock = tmp_path.joinpath(*LOCK_PATH.parts)
        root_lock.write_text(
            yaml.safe_dump({
                "images": {"ALPINE_IMAGE": {"index": self.ALPINE_INDEX}},
                "bundles": {name: {"reference": reference} for name, reference in TUNNEL_BUNDLES.items()},
            }),
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
        (tmp_path / ".env").write_text("", encoding="utf-8")
        _write_lock(
            tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST}, TUNNEL_BUNDLES
        )
        monkeypatch.delenv("PLATFORM", raising=False)
        monkeypatch.setattr(up, "bash_handoff", _failing_handoff)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        kept = [
            Path(line.split(":", 1)[1].strip()) for line in result.output.splitlines() if "kept for inspection" in line
        ]
        assert len(kept) == 1
        assert kept[0].read_text(encoding="utf-8") == f"ALPINE_IMAGE={self.ALPINE_MANIFEST}\n"


class TestBundleLockEnforcement:
    def test_a_selected_ref_without_its_bundle_lock_entry_refuses_naming_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("", encoding="utf-8")
        _write_lock(tmp_path, entries={}, bundles={})
        monkeypatch.delenv("BUNDLES", raising=False)
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
        (tmp_path / ".env").write_text("", encoding="utf-8")
        relocked = dict(TUNNEL_BUNDLES)
        relocked["TUNNEL_BUNDLE"] = f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{PLATFORM_DIGEST}"
        _write_lock(tmp_path, entries={}, bundles=relocked)
        recorder = AssemblyRecorder()
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == ["oci://" + relocked["TUNNEL_BUNDLE"]]


OVERRIDE_PATH = "../producer/stack/generated/compose/compose.placeframe-cuda.yaml"


class TestBundleOverrides:
    def _invoke_up(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> ProducerEnvRecorder:
        recorder = ProducerEnvRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(root)
        monkeypatch.delenv("BUNDLES", raising=False)
        monkeypatch.delenv("PLATFORM", raising=False)
        return recorder

    def test_the_path_form_replaces_the_include_and_fills_from_the_producer_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        output = _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64"]))
        _write_override_consumer(
            consumer,
            [f"BUNDLE_OVERRIDE_PLACEFRAME_CUDA={OVERRIDE_PATH}"],
            platforms=["linux/amd64"],
        )
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == [output.resolve().as_posix()]
        assert [_normalize(command, consumer) for command in recorder.commands] == [
            "docker compose -f ASSEMBLY --project-directory ROOT --env-file .env --env-file LOCKENV up --pull missing -d"
        ]
        assert recorder.env_contents == [
            (
                f"PLACEFRAME_CUDA_ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA_LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
            )
        ]

    def test_consumer_and_producer_locks_coexist_through_namespaced_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64"]))
        _write_override_consumer(
            consumer,
            [f"BUNDLE_OVERRIDE_PLACEFRAME_CUDA={OVERRIDE_PATH}"],
            platforms=["linux/amd64"],
            lock_images={"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + CONSUMER_ALPINE_DIGEST},
        )
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert recorder.env_contents == [
            (
                f"ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{CONSUMER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA_ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA_LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
            )
        ]

    def test_the_producer_lock_selects_the_named_platform(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64", "linux/arm64"]))
        _write_override_consumer(
            consumer,
            [f"BUNDLE_OVERRIDE_PLACEFRAME_CUDA={OVERRIDE_PATH}", "PLATFORM=linux/arm64"],
        )
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert recorder.env_contents == [
            (
                f"PLACEFRAME_CUDA_ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA_LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_ARM64_DIGEST}\n"
            )
        ]

    def test_a_multi_platform_producer_lock_without_a_selection_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64", "linux/arm64"]))
        _write_override_consumer(consumer, [f"BUNDLE_OVERRIDE_PLACEFRAME_CUDA={OVERRIDE_PATH}"])
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "the producer lock records platforms" in message
        assert "linux/amd64" in message and "linux/arm64" in message

    def test_the_path_form_requires_a_value(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text("BUNDLE_OVERRIDE_TUNNEL=\n", encoding="utf-8")
        self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "empty" in str(result.exception)

    def test_a_url_scheme_override_value_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        (tmp_path / ".env").write_text(
            "BUNDLE_OVERRIDE_TUNNEL=oci://ghcr.io/org/livekit:some-tag\n", encoding="utf-8"
        )
        self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "carries a URL scheme" in str(result.exception)

    def test_an_unknown_override_name_refuses_listing_the_vocabulary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_override_consumer(
            consumer, ["BUNDLE_OVERRIDE_GHOST=../producer/stack/generated/compose/compose.ghost.yaml"]
        )
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "BUNDLE_OVERRIDE_* names ['ghost']" in message
        assert "placeframe-cuda" in message

    def test_an_override_naming_a_local_entry_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_override_consumer(
            consumer,
            ["BUNDLE_OVERRIDE_EXTRA=../producer/stack/generated/compose/compose.extra.yaml"],
            services_extra={"extra": {"workloads": ["extra.yaml"]}},
        )
        _write_workload(consumer, "extra.yaml", "extra")
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "BUNDLE_OVERRIDE_* names ['extra']" in message
        assert "an override targets a ref entry" in message
        assert "placeframe-cuda" in message

    def test_an_unselected_override_is_inert(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        output = _write_producer(tmp_path, ["grafana"])
        _write_override_consumer(
            consumer,
            [
                f"BUNDLE_OVERRIDE_PLACEFRAME_CUDA={OVERRIDE_PATH}",
                "BUNDLE_OVERRIDE_PLACEFRAME_ROCM=../producer/stack/generated/compose/compose.placeframe-rocm.yaml",
            ],
        )
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == [output.resolve().as_posix()]
        assert recorder.env_contents == []

    def test_a_missing_override_path_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_override_consumer(consumer, [f"BUNDLE_OVERRIDE_PLACEFRAME_CUDA={OVERRIDE_PATH}"])
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "does not exist" in str(result.exception)

    def test_an_override_outside_the_compose_output_layout_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api"])
        stray = tmp_path / "producer" / "compose.placeframe-cuda.yaml"
        stray.write_text(yaml.safe_dump({"services": {"api": {"image": "demo/api:1"}}}), encoding="utf-8")
        _write_override_consumer(consumer, ["BUNDLE_OVERRIDE_PLACEFRAME_CUDA=../producer/compose.placeframe-cuda.yaml"])
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "stack/generated/compose/" in str(result.exception)

    def test_an_override_defining_the_edge_service_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        # A hand-authored override file outside the namespace scheme can still spell the
        # reserved service name; the refusal guards that hand-written surface.
        producer_output = tmp_path / "producer" / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml"
        producer_output.parent.mkdir(parents=True, exist_ok=True)
        producer_output.write_text(
            yaml.safe_dump({"services": {"api": {"image": "demo/api:1"}, "edge": {"image": "demo/edge:1"}}}),
            encoding="utf-8",
        )
        _write_override_consumer(consumer, [f"BUNDLE_OVERRIDE_PLACEFRAME_CUDA={OVERRIDE_PATH}"])
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "reserved service 'edge'" in str(result.exception)

    def test_an_override_sharing_a_service_name_with_another_output_namespaces_apart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64"]))
        _write_override_consumer(
            consumer,
            [f"BUNDLE_OVERRIDE_PLACEFRAME_CUDA={OVERRIDE_PATH}"],
            services_extra={"extra": {"workloads": ["grafana.yaml"]}},
        )
        _write_workload(consumer, "grafana.yaml", "grafana")
        _write_bundle_output(consumer, "extra", ["api"])
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert sorted(document["include"]) == sorted([
            (tmp_path / "producer" / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml")
            .resolve()
            .as_posix(),
            (consumer / "stack" / "generated" / "compose" / "compose.extra.yaml").resolve().as_posix(),
        ])


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
