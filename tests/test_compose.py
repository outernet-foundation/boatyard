import platform
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from bashrun.bash import bash, bash_output
from typer.testing import CliRunner

from boatyard.cli import app
from boatyard import dockerfiles as dockerfiles_module
from boatyard.verbs import down, up
from boatyard.config.lock import LOCK_PATH

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
ALPINE_BASE_IMAGES = {"ALPINE_IMAGE": "docker.io/library/alpine:3.20"}
PINNED_COMPOSE_VERSION = "5.6.1"

ASSEMBLY_ARGUMENT_PATTERN = re.compile(r"-f (\S*devkit-assembly-\S+\.yaml)")
LOCK_ENV_ARGUMENT_PATTERN = re.compile(r"(\S*devkit-env-\S+\.env)")

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


def _write_config(root: Path, sections: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"requires": ">=0.0", "project_name": "demo"}
    if sections is not None:
        payload.update(sections)
    payload.setdefault("services", {"ext": {"ref": LIVEKIT_REF}})
    if "services" in payload and "gateway" not in payload:
        payload["gateway"] = 8080
    if payload.get("gateway") is not None:
        payload.setdefault("platforms", ["linux/amd64"])
        payload["base_images"] = {
            "CADDY_IMAGE": "docker.io/library/caddy:2.8.4-alpine",
            **payload.get("base_images", {}),
        }
    (root / "boatyard.yml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _write_ref_tree(root: Path, base_images: dict[str, str] | None = None) -> None:
    # Write the minimal services-declaring repo: one bare remote include.
    payload: dict[str, object] = {
        "project_name": "demo",
        "platforms": ["linux/amd64"],
        "services": {"tunnel": {"ref": LIVEKIT_REF}},
    }
    if base_images is not None:
        payload["base_images"] = base_images
    _write_config(root, payload)
    _write_lock(root, entries={}, bundles=TUNNEL_BUNDLES)


def _normalize(command: str, root: Path) -> str:
    command = re.sub(r"\S*devkit-assembly-\S+\.yaml", "ASSEMBLY", command)
    command = re.sub(r"\S*devkit-env-\S+\.env", "LOCKENV", command)
    return command.replace(str(root), "ROOT")


def _write_lock(
    root: Path,
    entries: dict[str, str] | None = None,
    platform_entries: dict[str, str] | None = None,
    bundles: dict[str, str] | None = None,
) -> None:
    # Write one platform-manifest reference per declared platform beside the index.
    declared = {
        "CADDY_IMAGE": MIRROR_PREFIX + "/docker.io/library/caddy:2.8.4-alpine@" + FAKE_DIGEST,
        **(entries or {}),
    }
    platforms = platform_entries if platform_entries is not None else {"linux/amd64": ""}
    document: dict[str, object] = {
        "platforms": list(platforms),
        "images": {
            name: {
                "index": reference,
                "platforms": {platform: manifest or reference for platform, manifest in platforms.items()},
            }
            for name, reference in declared.items()
        },
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


def _write_producer(root: Path, services: list[str], lock: dict[str, Any] | None = None) -> Path:
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
        producer_root = root / "producer"
        producer_score = producer_root / "stack" / "score"
        producer_score.mkdir(parents=True, exist_ok=True)
        (producer_score / "api.yaml").write_text("metadata:\n  name: api\ncontainers:\n  api: {}\n", encoding="utf-8")
        (producer_root / "boatyard.yml").write_text(
            yaml.safe_dump({
                "requires": ">=0.0",
                "project_name": "producer",
                "repository": "ghcr.io/org/producer",
                "services": {"core": {"workloads": ["api.yaml"]}},
                "base_images": {
                    "LOKI_IMAGE": "docker.io/grafana/loki:3.0",
                    "ALPINE_IMAGE": "docker.io/library/alpine:3.20",
                },
                "platforms": list(lock.get("platforms", [])),
            }),
            encoding="utf-8",
        )
        producer_root.joinpath(*LOCK_PATH.parts).write_text(yaml.safe_dump(lock), encoding="utf-8")
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
        "platforms": list(platforms),
        "images": {
            "LOKI_IMAGE": {
                "index": f"{MIRROR_PREFIX}/docker.io/library/loki:3.0@{FAKE_DIGEST}",
                "platforms": loki_manifests,
            },
            "ALPINE_IMAGE": {
                "index": f"{MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}",
                "platforms": alpine_manifests,
            },
        },
    }


def _write_values_file(root: Path, values: dict[str, object]) -> None:
    (root / "boatyard.values.yaml").write_text(
        yaml.safe_dump(values, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _write_override_consumer(
    root: Path,
    values: dict[str, object] | None = None,
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
    images = lock_images if lock_images is not None else {}
    payload: dict[str, object] = {
        "project_name": "demo",
        "repository": "ghcr.io/org/demo",
        "services": services,
    }
    resolved_platforms = platforms if platforms is not None else ["linux/amd64"]
    payload["platforms"] = resolved_platforms
    if images:
        payload["base_images"] = dict.fromkeys(images, "docker.io/library/alpine:3.20")
    _write_config(root, payload)
    _write_lock(
        root,
        entries=dict(images),
        platform_entries=dict.fromkeys(resolved_platforms, ""),
    )
    _write_values_file(root, {"bundles": ["placeframe-cuda"], **(values or {})})


def _write_workload(root: Path, name: str, service_name: str) -> None:
    path = root / "stack" / "score" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"metadata": {"name": service_name}}), encoding="utf-8")


def _silent_handoff(_command: str) -> None:
    return None


def _producer_tree_shas(root: Path, builds: object) -> dict[str, str]:
    return {"API_SHA": "tree-producer"} if "producer" in root.parts else {"API_SHA": "tree-consumer"}


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


def _write_consumer_tree(
    root: Path,
    *,
    bundles: str | None = "placeframe-cuda,auth-keycloak",
    extra_values: dict[str, object] | None = None,
) -> Path:
    # Write the canonical consumer tree; bundles=None selects nothing.
    _write_config(
        root,
        {
            "project_name": "demo",
            "repository": "ghcr.io/org/demo",
            "platforms": ["linux/amd64"],
            "base_images": {"CADDY_IMAGE": "docker.io/library/caddy:2.8.4-alpine"},
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
    values: dict[str, object] = {
        "GATEWAY_PORT": 58080,
        "LIVEKIT_SIGNALING_PORT": 6178,
        "public_url": "http://192.168.8.10:58080",
    }
    if bundles is not None:
        values["bundles"] = [name for name in bundles.split(",") if name]
    values.update(extra_values or {})
    _write_values_file(root, {key: value for key, value in values.items() if value is not None})
    return cuda


class TestSelectionLaw:
    def _invoke_up(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        bundles: str | None = "placeframe-cuda,auth-keycloak",
    ) -> AssemblyRecorder:
        _write_consumer_tree(tmp_path, bundles=bundles)
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
        _write_lock(tmp_path, entries={})
        one_cuda = _write_bundle_output(tmp_path, "one-cuda", ["api"])
        _write_bundle_output(tmp_path, "two-cuda", ["edge"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_workload(tmp_path, "edge.yaml", "edge")
        _write_values_file(tmp_path, {"bundles": ["one-cuda", "two-cuda"]})
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
                "base_images": {"CADDY_IMAGE": "docker.io/library/caddy:2.8.4-alpine"},
                "gateway": 8080,
                "services": {
                    "livekit": {
                        "ref": LIVEKIT_REF,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                        "ports": [{"service": "livekit", "published": 6178, "target": 6178}],
                    }
                },
            },
        )
        _write_lock(tmp_path, bundles={"LIVEKIT_BUNDLE": LIVEKIT_PINNED})
        _write_values_file(tmp_path, {})
        recorder = AssemblyRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == ["oci://" + LIVEKIT_PINNED]

    def test_an_options_only_repo_with_an_empty_selection_hands_compose_an_edge_only_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "base_images": {"CADDY_IMAGE": "docker.io/library/caddy:2.8.4-alpine"},
                "gateway": 8080,
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
        _write_values_file(tmp_path, {})
        recorder = AssemblyRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["name"] == "demo"
        assert list(document) == ["name", "configs", "services"]
        assert document["services"]["edge"]["ports"] == [{"target": 8443, "published": 8080, "protocol": "tcp"}]
        assert document["configs"]["devkit-edge-caddyfile"]["content"] == ":8443 {\n}\n"

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

    def test_a_shell_selection_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        recorder = self._invoke_up(tmp_path, monkeypatch)
        monkeypatch.setenv("BUNDLES", "placeframe-rocm,auth-keycloak")

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert "placeframe-cuda-net" in document["services"]["edge"]["networks"]

    def test_stray_selection_in_an_undeclaring_repo_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_values_file(tmp_path, {"bundles": ["cuda"]})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, FileNotFoundError)

    def test_up_without_config_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_values_file(tmp_path, {"public_url": "https://example.dev"})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, FileNotFoundError)

    def test_up_in_a_services_less_repo_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / "boatyard.yml").write_text("requires: '>=0.0'\nproject_name: demo\n", encoding="utf-8")
        _write_values_file(tmp_path, {"public_url": "https://example.dev"})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "services is required" in str(result.exception)

    def test_missing_local_output_refuses_naming_generate_score(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        (tmp_path / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml").unlink()
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
        assert edge["image"] == MIRROR_PREFIX + "/docker.io/library/caddy:2.8.4-alpine@" + FAKE_DIGEST
        assert edge["ports"] == [{"target": 8443, "published": "${GATEWAY_PORT:?err}", "protocol": "tcp"}]
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
        tmp_path.joinpath(*LOCK_PATH.parts).write_text(
            yaml.safe_dump({
                "platforms": ["linux/amd64"],
                "images": {},
                "bundles": {
                    "AUTH_KEYCLOAK_BUNDLE": {"reference": KEYCLOAK_PINNED},
                    "LIVEKIT_BUNDLE": {"reference": LIVEKIT_PINNED},
                },
            }),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "CADDY_IMAGE" in str(result.exception)

    def test_an_auth_selection_without_public_url_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path, bundles="placeframe-cuda,auth-keycloak", extra_values={"public_url": None})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "public_url" in str(result.exception)


class TestUpValidation:
    def _invoke_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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

    def test_a_local_port_owner_absent_from_the_output_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_bundle_output(tmp_path, "placeframe-cuda", ["api"])
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "platforms": ["linux/amd64"],
                "gateway": 8080,
                "services": {
                    "placeframe": {
                        "options": {
                            "cuda": {
                                "workloads": ["api.yaml"],
                                "ports": [{"service": "grafana", "published": 3306, "target": 3306}],
                            }
                        }
                    }
                },
            },
        )
        _write_workload(tmp_path, "api.yaml", "api")
        _write_lock(tmp_path, entries={})
        _write_values_file(tmp_path, {"bundles": ["placeframe-cuda"]})

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "'3306' references 'placeframe-cuda-grafana'" in str(result.exception)

    def test_a_local_output_defining_the_edge_service_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # A hand-edited generated output can spell the reserved name; the scan refuses it at bring-up.
        cuda = _write_consumer_tree(tmp_path)
        cuda.write_text(
            yaml.safe_dump({
                "services": {
                    "placeframe-cuda-api": {"image": "demo/api:1"},
                    "placeframe-cuda-grafana": {"image": "demo/grafana:1"},
                    "edge": {"image": "demo/edge:1"},
                }
            }),
            encoding="utf-8",
        )

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "reserved service 'edge'" in str(result.exception)

    def test_a_local_output_slot_no_environment_source_fills_refuses(
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

    def test_a_tree_slot_requirement_is_satisfied_by_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path)
        cuda = tmp_path / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml"
        cuda.write_text(
            "services:\n"
            "  placeframe-cuda-api:\n"
            "    image: demo/api:${PLACEFRAME_CUDA__BOATYARD_TREE__API_SHA}\n"
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

        assert result.exit_code == 0, result.output

    def test_a_requirement_filled_by_the_env_file_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path, extra_values={"LIVEKIT_TOKEN": "set"})
        root_lock = tmp_path.joinpath(*LOCK_PATH.parts)
        root_lock.write_text(
            root_lock.read_text(encoding="utf-8").replace(
                f"  LIVEKIT_BUNDLE:\n    reference: {LIVEKIT_PINNED}",
                f"  LIVEKIT_BUNDLE:\n    reference: {LIVEKIT_PINNED}\n    requires:\n      - LIVEKIT_TOKEN",
            ),
            encoding="utf-8",
        )

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output

    def test_an_unfilled_requirement_set_in_the_shell_is_named_as_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path)
        monkeypatch.setenv("API_TOKEN", "shell-value")
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
        assert "API_TOKEN set in the shell and ignored" in str(result.exception)


class TestSharedNames:
    def test_two_selected_outputs_sharing_a_service_name_namespace_apart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "base_images": {"CADDY_IMAGE": "docker.io/library/caddy:2.8.4-alpine"},
                "gateway": 8080,
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
        _write_values_file(tmp_path, {"bundles": ["left-a", "right-b"]})
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
                "base_images": {"CADDY_IMAGE": "docker.io/library/caddy:2.8.4-alpine"},
                "gateway": 8080,
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
        _write_values_file(tmp_path, {"bundles": ["left-a", "right-b"]})
        recorder = AssemblyRecorder()
        monkeypatch.setattr(down, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert recorder.documents == [{"name": "demo"}]


class TestDownTeardown:
    def test_down_hands_off_a_name_only_root_with_remove_orphans(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        recorder = AssemblyRecorder()
        monkeypatch.setattr(down, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert len(recorder.commands) == 1
        command = recorder.commands[0]
        assert command.startswith("docker compose -f ")
        assert "--env-file" not in command
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
        recorder = HandoffRecorder()
        monkeypatch.setattr(down, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down", "--volumes"])

        assert result.exit_code == 0, result.output
        assert recorder.commands[0].endswith("down --remove-orphans -v")

    def test_down_without_config_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_values_file(tmp_path, {"public_url": "https://example.dev"})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code != 0
        assert isinstance(result.exception, FileNotFoundError)

    def test_down_tears_down_by_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path)
        _write_values_file(tmp_path, {"public_url": "https://example.dev"})
        recorder = AssemblyRecorder()
        monkeypatch.setattr(down, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert recorder.documents == [{"name": "demo"}]


class TestLockRequirement:
    def test_a_repo_without_a_lock_skips_the_probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "gateway": None,
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_values_file(tmp_path, {"bundles": ["apps-core"]})
        recorder = HandoffRecorder()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(up, "bash_handoff", recorder.run)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert [_normalize(command, tmp_path) for command in recorder.commands] == [
            "docker compose -f ASSEMBLY --project-directory ROOT --env-file LOCKENV up --pull missing -d"
        ]


class TestLockEnvSynthesis:
    ALPINE_INDEX = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST
    ALPINE_MANIFEST = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + PLATFORM_DIGEST

    def _invoke_up_with_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: HandoffRecorder):
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)
        return runner.invoke(app, ["up"])

    def test_the_attached_env_file_carries_the_platform_manifest_not_the_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "base_images": ALPINE_BASE_IMAGES,
                "repository": "ghcr.io/org/demo",
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST})
        _write_values_file(tmp_path, {"bundles": ["apps-core"]})
        recorder = LockEnvRecorder()

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0, result.output
        [content] = recorder.env_contents
        assert content == (
            f"# boatyard.lock.yml\n"
            f"APPS_CORE__BOATYARD_LOCK__ALPINE_IMAGE={self.ALPINE_MANIFEST}\n"
            f"APPS_CORE__BOATYARD_LOCK__CADDY_IMAGE={self.ALPINE_MANIFEST}\n"
        )

    def test_a_selected_local_entry_reads_its_lock_keys_under_the_entry_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "platforms": ["linux/amd64"],
                "base_images": ALPINE_BASE_IMAGES,
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST})
        _write_values_file(tmp_path, {"bundles": ["apps-core"]})
        recorder = LockEnvRecorder()

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0, result.output
        [content] = recorder.env_contents
        assert content == (
            f"# boatyard.lock.yml\nAPPS_CORE__BOATYARD_LOCK__ALPINE_IMAGE={self.ALPINE_MANIFEST}\nAPPS_CORE__BOATYARD_LOCK__CADDY_IMAGE={self.ALPINE_MANIFEST}\n"
        )

    def test_a_selected_local_entry_carries_its_tree_tags_under_the_entry_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "base_images": ALPINE_BASE_IMAGES,
                "repository": "ghcr.io/org/demo",
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST})
        _write_values_file(tmp_path, {"bundles": ["apps-core"]})
        monkeypatch.setattr(dockerfiles_module, "compute_service_shas", _producer_tree_shas)
        recorder = LockEnvRecorder()

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0, result.output
        [content] = recorder.env_contents
        assert content == (
            f"# boatyard.lock.yml\n"
            f"APPS_CORE__BOATYARD_LOCK__ALPINE_IMAGE={self.ALPINE_MANIFEST}\n"
            f"APPS_CORE__BOATYARD_LOCK__CADDY_IMAGE={self.ALPINE_MANIFEST}\n"
            f"# tree tags\n"
            f"APPS_CORE__BOATYARD_TREE__API_SHA=tree-consumer\n"
        )
        assert " -u APPS_CORE__BOATYARD_TREE__API_SHA " in recorder.commands[0]

    def test_two_consecutive_syntheses_are_byte_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path, base_images=ALPINE_BASE_IMAGES)
        _write_values_file(tmp_path, {})
        _write_lock(
            tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST}, TUNNEL_BUNDLES
        )
        recorder = LockEnvRecorder()

        first = self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)
        assert first.exit_code == 0, first.output
        self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert len(recorder.env_contents) == 2
        assert recorder.env_contents[0] == recorder.env_contents[1]

    def test_a_multi_platform_repo_selects_the_named_platform(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64", "linux/arm64"],
                "base_images": ALPINE_BASE_IMAGES,
                "repository": "ghcr.io/org/demo",
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        arm64_manifest = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + PLATFORM_DIGEST
        _write_lock(
            tmp_path,
            {"ALPINE_IMAGE": self.ALPINE_INDEX},
            {"linux/amd64": self.ALPINE_MANIFEST, "linux/arm64": arm64_manifest},
        )
        _write_values_file(tmp_path, {"bundles": ["apps-core"], "platform": "linux/arm64"})
        recorder = LockEnvRecorder()

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, recorder)

        assert result.exit_code == 0, result.output
        assert recorder.env_contents[0] == (
            f"# boatyard.lock.yml\n"
            f"APPS_CORE__BOATYARD_LOCK__ALPINE_IMAGE={arm64_manifest}\n"
            f"APPS_CORE__BOATYARD_LOCK__CADDY_IMAGE={arm64_manifest}\n"
        )

    def test_a_multi_platform_repo_without_a_selection_refuses_listing_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64", "linux/arm64"],
                "base_images": ALPINE_BASE_IMAGES,
                "services": {"tunnel": {"ref": LIVEKIT_REF}},
            },
        )
        _write_values_file(tmp_path, {})
        _write_lock(
            tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": "", "linux/arm64": ""}, TUNNEL_BUNDLES
        )

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, HandoffRecorder())

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "platform: must be named" in message
        assert "linux/amd64" in message and "linux/arm64" in message

    def test_an_undeclared_platform_selection_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path, base_images=ALPINE_BASE_IMAGES)
        _write_values_file(tmp_path, {"platform": "linux/ppc64le"})
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, bundles=TUNNEL_BUNDLES)

        result = self._invoke_up_with_lock(tmp_path, monkeypatch, HandoffRecorder())

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "linux/ppc64le" in message
        assert "linux/amd64" in message

    def test_a_failed_run_keeps_the_env_file_and_never_echoes_its_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path, base_images=ALPINE_BASE_IMAGES)
        _write_values_file(tmp_path, {"SECRET_TOKEN": "supersecret"})
        _write_lock(
            tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST}, TUNNEL_BUNDLES
        )
        monkeypatch.setattr(up, "bash_handoff", _failing_handoff)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        kept = [
            Path(line.split(":", 1)[1].strip()) for line in result.output.splitlines() if "kept for inspection" in line
        ]
        assert len(kept) == 1
        assert kept[0].read_text(encoding="utf-8") == "# boatyard.values.yaml\nSECRET_TOKEN=supersecret\n"
        assert "supersecret" not in result.output


class TestBundleLockEnforcement:
    def test_a_selected_ref_without_its_bundle_lock_entry_refuses_naming_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {})
        _write_lock(tmp_path, entries={}, bundles={})
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
        _write_values_file(tmp_path, {})
        relocked = dict(TUNNEL_BUNDLES)
        relocked["TUNNEL_BUNDLE"] = f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{PLATFORM_DIGEST}"
        _write_lock(tmp_path, entries={}, bundles=relocked)
        recorder = AssemblyRecorder()
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
        return recorder

    def test_the_path_form_replaces_the_include_and_fills_from_the_producer_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        output = _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64"]))
        _write_override_consumer(
            consumer,
            {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}},
            platforms=["linux/amd64"],
        )
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == [output.resolve().as_posix()]
        assert [_normalize(command, consumer) for command in recorder.commands] == [
            (
                "env -u PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE -u PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE "
                "docker compose -f ASSEMBLY --project-directory ROOT --env-file LOCKENV up --pull missing -d"
            )
        ]
        assert recorder.env_contents == [
            (
                "# boatyard.lock.yml\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
            )
        ]

    def test_the_producer_lock_fills_the_overridden_entrys_fan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64"]))
        _write_override_consumer(
            consumer,
            {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}},
            platforms=["linux/amd64"],
            lock_images={"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + CONSUMER_ALPINE_DIGEST},
        )
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert recorder.env_contents == [
            (
                "# boatyard.lock.yml\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
            )
        ]

    def test_the_producer_tree_fills_the_overridden_entrys_fan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64"]))
        _write_override_consumer(
            consumer,
            {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}},
            platforms=["linux/amd64"],
        )
        monkeypatch.setattr(dockerfiles_module, "compute_service_shas", _producer_tree_shas)
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert recorder.env_contents == [
            (
                "# boatyard.lock.yml\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
                "# tree tags\n"
                "PLACEFRAME_CUDA__BOATYARD_TREE__API_SHA=tree-producer\n"
            )
        ]

    def test_the_producer_lock_selects_the_named_platform(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64", "linux/arm64"]))
        _write_override_consumer(
            consumer,
            {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}, "platform": "linux/arm64"},
            platforms=["linux/amd64", "linux/arm64"],
        )
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert recorder.env_contents == [
            (
                "# boatyard.lock.yml\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_ARM64_DIGEST}\n"
            )
        ]

    def test_a_multi_platform_producer_lock_without_a_selection_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64", "linux/arm64"]))
        _write_override_consumer(consumer, {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}})
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "platforms ['linux/amd64', 'linux/arm64'] are recorded" in message
        assert "platform: must be named" in message

    def test_the_path_form_requires_a_value(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {"path_overrides": {"tunnel": "  "}})
        self._invoke_up(monkeypatch, tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "empty" in str(result.exception)

    def test_a_url_scheme_override_value_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {"path_overrides": {"tunnel": "oci://ghcr.io/org/livekit:some-tag"}})
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
            consumer, {"path_overrides": {"ghost": "../producer/stack/generated/compose/compose.ghost.yaml"}}
        )
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "path_overrides: names ['ghost']" in message
        assert "placeframe-cuda" in message

    def test_an_override_naming_a_local_entry_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_override_consumer(
            consumer,
            {"path_overrides": {"extra": "../producer/stack/generated/compose/compose.extra.yaml"}},
            services_extra={"extra": {"workloads": ["extra.yaml"]}},
        )
        _write_workload(consumer, "extra.yaml", "extra")
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "path_overrides: names [\x27extra\x27]" in message
        assert "an override targets a ref entry" in message
        assert "placeframe-cuda" in message

    def test_an_unselected_override_is_inert(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        output = _write_producer(tmp_path, ["grafana"], _producer_lock(["linux/amd64"]))
        _write_override_consumer(
            consumer,
            {
                "path_overrides": {
                    "placeframe-cuda": OVERRIDE_PATH,
                    "placeframe-rocm": "../producer/stack/generated/compose/compose.placeframe-rocm.yaml",
                }
            },
        )
        recorder = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [document] = recorder.documents
        assert document["include"] == [output.resolve().as_posix()]
        assert recorder.env_contents == [
            (
                "# boatyard.lock.yml\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
            )
        ]

    def test_an_override_with_no_repo_root_above_it_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        output = tmp_path / "loose" / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml"
        output.parent.mkdir(parents=True)
        output.write_text("services: {}\n", encoding="utf-8")
        _write_override_consumer(
            consumer,
            {"path_overrides": {"placeframe-cuda": "../loose/stack/generated/compose/compose.placeframe-cuda.yaml"}},
        )
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "no boatyard.yml-bearing repo root" in str(result.exception)

    def test_a_missing_override_path_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_override_consumer(consumer, {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}})
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
        _write_override_consumer(
            consumer, {"path_overrides": {"placeframe-cuda": "../producer/compose.placeframe-cuda.yaml"}}
        )
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "stack/generated/compose/" in str(result.exception)

    def test_an_override_defining_the_edge_service_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        # A hand-authored file outside the namespace scheme can still spell the
        # reserved service name; the refusal guards every locally included bundle file.
        producer_output = _write_producer(tmp_path, ["api"], _producer_lock(["linux/amd64"]))
        producer_output.write_text(
            yaml.safe_dump({"services": {"api": {"image": "demo/api:1"}, "edge": {"image": "demo/edge:1"}}}),
            encoding="utf-8",
        )
        _write_override_consumer(consumer, {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}})
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
            {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}},
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


class TestValuesFileLaw:
    def test_the_scrub_carries_requirement_names(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path, extra_values={"LIVEKIT_TOKEN": "set"})
        root_lock = tmp_path.joinpath(*LOCK_PATH.parts)
        root_lock.write_text(
            root_lock.read_text(encoding="utf-8").replace(
                f"  LIVEKIT_BUNDLE:\n    reference: {LIVEKIT_PINNED}",
                f"  LIVEKIT_BUNDLE:\n    reference: {LIVEKIT_PINNED}\n    requires:\n      - LIVEKIT_TOKEN",
            ),
            encoding="utf-8",
        )
        recorder = HandoffRecorder()
        monkeypatch.setattr(up, "bash_handoff", recorder.run)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert " -u LIVEKIT_TOKEN " in recorder.commands[0]

    def test_a_values_key_spelling_the_lock_marker_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path, base_images=ALPINE_BASE_IMAGES)
        _write_values_file(
            tmp_path, {"TUNNEL__BOATYARD_LOCK__ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20"}
        )
        _write_lock(
            tmp_path,
            {"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST},
            bundles=TUNNEL_BUNDLES,
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "carry a reserved __BOATYARD_LOCK__/__BOATYARD_TREE__ marker" in str(result.exception)
        assert "never set them by hand" in str(result.exception)

    def test_a_values_key_spelling_the_tree_marker_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path, base_images=ALPINE_BASE_IMAGES)
        _write_values_file(tmp_path, {"TUNNEL__BOATYARD_TREE__API_SHA": "tree-abc"})
        _write_lock(
            tmp_path,
            {"ALPINE_IMAGE": MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST},
            bundles=TUNNEL_BUNDLES,
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "carry a reserved __BOATYARD_LOCK__/__BOATYARD_TREE__ marker" in str(result.exception)
        assert "never set them by hand" in str(result.exception)

    def test_a_values_key_outside_the_env_name_grammar_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {"lowercase_key": "value"})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "not ALL_CAPS environment names" in str(result.exception)


class TestShellPrecedenceEndToEnd:
    def test_the_scrub_defeats_composes_shell_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned_compose: str
    ) -> None:
        compose_file = tmp_path / "compose.yaml"
        compose_file.write_text("services:\n  demo:\n    image: ${DEMO_TOKEN}-rendered\n", encoding="utf-8")
        env_file = tmp_path / "run.env"
        env_file.write_text("DEMO_TOKEN=from-file\n", encoding="utf-8")
        monkeypatch.setenv("DEMO_TOKEN", "from-shell")

        leaked = bash_output(
            f"{pinned_compose} -f {compose_file} --project-directory {tmp_path} "
            f"--env-file {env_file} config --format json"
        )
        assert "from-shell-rendered" in leaked

        scrubbed = bash_output(
            f"env -u DEMO_TOKEN {pinned_compose} -f {compose_file} "
            f"--project-directory {tmp_path} --env-file {env_file} config --format json"
        )
        assert "from-file-rendered" in scrubbed
