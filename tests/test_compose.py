import platform
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from bashrun.bash import CalledProcessError, bash, bash_output
from typer.testing import CliRunner

from boatyard.cli import app
from boatyard.models.image import Image
from boatyard.verbs import down, up
from boatyard.models.lock_file import LOCK_PATH

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
LIVEKIT_BUNDLE_ENTRY = {
    "reference": LIVEKIT_PINNED,
    "services": ["livekit"],
    "routes": [],
    "ports": [],
    "container_names": [],
}
AUTH_KEYCLOAK_BUNDLE_ENTRY = {
    "reference": KEYCLOAK_PINNED,
    "services": ["keycloak"],
    "routes": [],
    "ports": [],
    "container_names": [],
}
EDGE_BASE_URL_LINE = "EDGE_BASE_URL=http://boatyard-gateway:8443\n"
BARRIER_MINT_LINE = "BOATYARD_BARRIER_IMAGE=docker.io/library/alpine:3.20\n"
PINNED_COMPOSE_VERSION = "5.6.1"

ASSEMBLY_ARGUMENT_PATTERN = re.compile(r"-f (\S*boatyard-gateway-\S+\.yaml)")
LOCK_ENV_ARGUMENT_PATTERN = re.compile(r"(\S*boatyard-env-\S+\.env)")

KEYCLOAK_TEXT = "services:\n  keycloak:\n    image: demo/keycloak:1\n"
LIVEKIT_TEXT = "services:\n  livekit:\n    image: demo/livekit:1\n"


yaml.SafeLoader.add_constructor("!reset", lambda _loader, _node: None)


class StubArtifacts:
    def __init__(self, texts_by_reference: dict[str, list[str]] | None = None) -> None:
        self.texts_by_reference = texts_by_reference or {
            KEYCLOAK_PINNED: [KEYCLOAK_TEXT],
            LIVEKIT_PINNED: [LIVEKIT_TEXT],
        }
        self.fetched: list[str] = []

    def fetch_compose_texts(self, pinned: str) -> list[str]:
        self.fetched.append(pinned)
        return self.texts_by_reference[pinned]


class UpSpy:
    # Captures the per-project invocations: command order, project names, the shared env file
    # content, the gateway document, and each bundle's override document.
    def __init__(self, texts_by_reference: dict[str, list[str]] | None = None) -> None:
        self.commands: list[str] = []
        self.projects: list[str] = []
        self.env_content: str | None = None
        self.gateway_documents: list[dict[str, Any]] = []
        self.override_documents: list[dict[str, Any]] = []
        self.artifacts = StubArtifacts(texts_by_reference)

    def run(self, command: str) -> None:
        self.commands.append(command)
        self.projects.append(command.split(" -p ", 1)[1].split()[0])
        env_match = LOCK_ENV_ARGUMENT_PATTERN.search(command)
        if env_match is not None and self.env_content is None:
            self.env_content = Path(env_match.group(1)).read_text(encoding="utf-8")
        gateway_match = ASSEMBLY_ARGUMENT_PATTERN.search(command)
        if gateway_match is not None:
            self.gateway_documents.append(yaml.safe_load(Path(gateway_match.group(1)).read_text(encoding="utf-8")))
        for override in re.finditer(r"-f (\S*boatyard-override-\S+\.yaml)", command):
            self.override_documents.append(yaml.safe_load(Path(override.group(1)).read_text(encoding="utf-8")))

    def spy_up(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(up, "bash", self.run)
        monkeypatch.setattr(up, "bash_handoff", self.run)
        monkeypatch.setattr(up, "bash_check", _bash_check_true)
        monkeypatch.setattr(up, "install_crane", lambda: None)
        monkeypatch.setattr(up, "OciArtifactManifest", self.artifacts)
        return self

    def bundle_command(self, project: str) -> str:
        return next(command for command in self.commands if f"-p {project} " in command)

    @property
    def gateway(self) -> dict[str, Any]:
        [document] = self.gateway_documents
        return document


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
    if "services" in payload and "gateway_port" not in payload:
        payload["gateway_port"] = 8080
    if payload.get("gateway_port") is not None:
        payload.setdefault("platforms", ["linux/amd64"])
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


def _normalize(command: str, root: Path, producer: Path | None = None) -> str:
    command = re.sub(r"\S*boatyard-gateway-\S+\.yaml", "GATEWAY", command)
    command = re.sub(r"\S*boatyard-override-\S+\.yaml", "OVERRIDE", command)
    command = re.sub(r"\S*boatyard-bundle-\S+\.yaml", "BUNDLE", command)
    command = re.sub(r"\S*boatyard-env-\S+\.env", "LOCKENV", command)
    if producer is not None:
        command = command.replace(producer.resolve().as_posix(), "PRODUCER")
    return command.replace(str(root), "ROOT")


def _write_lock(
    root: Path,
    entries: dict[str, str] | None = None,
    platform_entries: dict[str, str] | None = None,
    bundles: Mapping[str, str | dict[str, object]] | None = None,
) -> None:
    # Write one platform-manifest reference per declared platform beside the index.
    declared = dict(entries or {})
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
        document["bundles"] = {
            name: (
                {"reference": reference, "services": [], "routes": [], "ports": [], "container_names": []}
                if isinstance(reference, str)
                else {
                    "services": [],
                    "routes": [],
                    "ports": [],
                    "container_names": [],
                    **reference,
                }
            )
            for name, reference in bundles.items()
        }
    root.joinpath(*LOCK_PATH.parts).write_text(
        yaml.safe_dump(document),
        encoding="utf-8",
    )


def _write_bundle_output(root: Path, name: str, services: list[str], environment: dict[str, str] | None = None) -> Path:
    path = root / "stack" / "generated" / "compose" / f"compose.{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({
            "services": {
                service: {
                    "image": f"demo/{service}:1",
                    **({"environment": dict(environment)} if environment is not None else {}),
                }
                for service in services
            },
        }),
        encoding="utf-8",
    )
    return path


def _write_producer(
    root: Path,
    services: list[str],
    lock: dict[str, Any] | None = None,
    built: bool = False,
) -> Path:
    # A sibling producer checkout carrying the fixed stack/generated/compose/ output layout.
    output = root / "producer" / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump({
            "services": {service: {"image": f"demo/{service}:1"} for service in services},
        }),
        encoding="utf-8",
    )
    if lock is not None:
        producer_root = root / "producer"
        producer_score = producer_root / "stack" / "score"
        producer_score.mkdir(parents=True, exist_ok=True)
        referenced: list[str] = []
        for service in services:
            if built:
                (producer_score / f"{service}.yaml").write_text(
                    "metadata:\n"
                    f"  name: {service}\n"
                    "  annotations:\n"
                    f"    boatyard/dockerfile: {service}=workloads/{service}/Dockerfile\n"
                    "containers:\n"
                    f"  {service}: {{}}\n",
                    encoding="utf-8",
                )
                image_directory = producer_root / "workloads" / service
                image_directory.mkdir(parents=True, exist_ok=True)
                (image_directory / "Dockerfile").write_text(
                    "ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\n", encoding="utf-8"
                )
                referenced.append(f"{service}.yaml")
        if not built:
            (producer_score / "api.yaml").write_text("metadata:\n  name: api\ncontainers: {}\n", encoding="utf-8")
        (producer_root / "boatyard.yml").write_text(
            yaml.safe_dump({
                "requires": ">=0.0",
                "project_name": "producer",
                "repository": "ghcr.io/org/producer",
                "services": {"core": {"options": {"main": {"workloads": referenced or ["api.yaml"]}}}},
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
    entries = dict(images)
    _write_lock(
        root,
        entries=entries,
        platform_entries=dict.fromkeys(resolved_platforms, ""),
    )
    _write_values_file(root, {"bundles": ["placeframe-cuda"], **(values or {})})


def _write_workload(root: Path, name: str, service_name: str, environment: dict[str, str] | None = None) -> None:
    # Metadata-only: the container surface under test lives in the bundle artifacts
    path = root / "stack" / "score" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {"metadata": {"name": service_name}}
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def _bash_check_true(_command: str) -> bool:
    return True


def _bash_check_false(_command: str) -> bool:
    return False


def _producer_tree_sha(self: Image, root: Path) -> str:
    return "tree-producer" if "producer" in root.parts else "tree-consumer"


class HandoffRecorder:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str) -> None:
        self.commands.append(command)


class AssemblyRecorder(HandoffRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[dict[str, Any]] = []
        self.env_contents: list[str] = []

    def run(self, command: str) -> None:
        super().run(command)
        match = ASSEMBLY_ARGUMENT_PATTERN.search(command)
        if match is not None:
            self.documents.append(yaml.safe_load(Path(match.group(1)).read_text(encoding="utf-8")))
        match = LOCK_ENV_ARGUMENT_PATTERN.search(command)
        if match is not None:
            self.env_contents.append(Path(match.group(1)).read_text(encoding="utf-8"))


def _write_consumer_tree(
    root: Path,
    *,
    bundles: str | None = "placeframe-cuda,auth-keycloak",
    extra_values: dict[str, object] | None = None,
    api_environment: dict[str, str] | None = None,
    bundle_environment: dict[str, str] | None = None,
) -> Path:
    # Write the canonical consumer tree; bundles=None selects nothing. bundle_environment
    # spells what generate-score rendered into the artifact (bare slots expand at render).
    _write_config(
        root,
        {
            "project_name": "demo",
            "repository": "ghcr.io/org/demo",
            "platforms": ["linux/amd64"],
            "gateway_port": 58080,
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
                        "cuda": {"workloads": ["api.yaml"], "vars": {"TORCH_DEVICE": "cuda"}},
                        "rocm": {"workloads": ["api.yaml"], "vars": {"TORCH_DEVICE": "rocm"}},
                    },
                    "routes": [
                        {"path": "/grafana/*", "upstream": "grafana:3000"},
                        {"path": "/*", "upstream": "api:8000"},
                    ],
                },
                "livekit": {
                    "ref": LIVEKIT_REF,
                    "ports": [
                        {"service": "livekit", "published": 6178, "target": 6178, "protocol": "tcp"},
                    ],
                },
            },
        },
    )
    _write_lock(
        root,
        bundles={
            "AUTH_KEYCLOAK_BUNDLE": dict(AUTH_KEYCLOAK_BUNDLE_ENTRY),
            "LIVEKIT_BUNDLE": dict(LIVEKIT_BUNDLE_ENTRY),
        },
    )
    rendered_environment = api_environment if bundle_environment is None else bundle_environment
    cuda = _write_bundle_output(root, "placeframe-cuda", ["api", "grafana"], environment=rendered_environment)
    _write_bundle_output(root, "placeframe-rocm", ["api", "grafana"], environment=rendered_environment)
    _write_workload(root, "api.yaml", "api", environment=api_environment)
    values: dict[str, object] = {"public_host": "192.168.8.10", "public_port": 58080}
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
    ) -> UpSpy:
        _write_consumer_tree(tmp_path, bundles=bundles)
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)
        return spy

    def test_options_of_different_groups_select_together(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        spy = self._invoke_up(tmp_path, monkeypatch)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo-auth-keycloak", "demo-placeframe-cuda", "demo-livekit", "demo"]
        cuda_command = spy.bundle_command("demo-placeframe-cuda")
        assert (tmp_path / "stack/generated/compose/compose.placeframe-cuda.yaml").resolve().as_posix() in (
            cuda_command
        )
        assert spy.artifacts.fetched == sorted([KEYCLOAK_PINNED, LIVEKIT_PINNED])

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
        _write_values_file(tmp_path, {"bundles": ["one-cuda", "two-cuda"]})
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo-one-cuda", "demo-two-cuda", "demo"]
        assert one_cuda.resolve().as_posix() in spy.bundle_command("demo-one-cuda")
        assert (tmp_path / "stack/generated/compose/compose.two-cuda.yaml").resolve().as_posix() in (
            spy.bundle_command("demo-two-cuda")
        )

    def test_zero_selection_is_legal_when_bare_entries_and_routes_carry_the_assembly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "gateway_port": 8080,
                "services": {
                    "livekit": {
                        "ref": LIVEKIT_REF,
                        "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                        "ports": [{"service": "livekit", "published": 6178, "target": 6178}],
                    }
                },
            },
        )
        _write_lock(tmp_path, bundles={"LIVEKIT_BUNDLE": dict(LIVEKIT_BUNDLE_ENTRY)})
        _write_values_file(tmp_path, {})
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo-livekit", "demo"]
        assert spy.artifacts.fetched == [LIVEKIT_PINNED]
        assert "boatyard-bundle-" in spy.bundle_command("demo-livekit")

    def test_an_options_only_repo_with_an_empty_selection_hands_compose_an_edge_only_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "gateway_port": 8080,
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
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo"]
        gateway = spy.gateway
        assert gateway["name"] == "demo"
        assert gateway["networks"] == {"demo-shared": {"external": True}}
        assert gateway["services"]["boatyard-gateway"]["ports"] == [
            {"target": 8443, "published": 8080, "protocol": "tcp"}
        ]
        assert gateway["configs"]["boatyard-gateway-caddyfile"]["content"] == ":8443 {\n}\n"

    def test_unknown_bundle_name_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch, bundles="placeframe-cuda,ghost")

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "names ['ghost'] are not among the selectable options" in str(result.exception)

    def test_a_bare_entry_name_is_not_selectable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch, bundles="livekit")

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "are not among the selectable options" in str(result.exception)

    def test_two_options_of_one_group_refuse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._invoke_up(tmp_path, monkeypatch, bundles="placeframe-cuda,placeframe-rocm")

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "are two options of one group (placeframe)" in str(result.exception)

    def test_a_shell_selection_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        spy = self._invoke_up(tmp_path, monkeypatch)
        monkeypatch.setenv("BUNDLES", "placeframe-rocm,auth-keycloak")

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo-auth-keycloak", "demo-placeframe-cuda", "demo-livekit", "demo"]

    def test_stray_selection_in_an_undeclaring_repo_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_values_file(tmp_path, {"bundles": ["cuda"]})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, FileNotFoundError)

    def test_up_without_config_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_values_file(tmp_path, {"public_host": "example.dev"})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, FileNotFoundError)

    def test_up_in_a_services_less_repo_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / "boatyard.yml").write_text("requires: '>=0.0'\nproject_name: demo\n", encoding="utf-8")
        _write_values_file(tmp_path, {"public_host": "example.dev"})
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
        assert "lacks its bundle output" in str(result.exception)
        assert "compose.placeframe-cuda.yaml" in str(result.exception)


class TestUpMaterialization:
    def test_the_gateway_and_override_documents_carry_the_assembly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_consumer_tree(tmp_path)
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo-auth-keycloak", "demo-placeframe-cuda", "demo-livekit", "demo"]
        gateway_service = spy.gateway["services"]["boatyard-gateway"]
        assert gateway_service["image"] == "docker.io/library/caddy:2.8.4-alpine"
        assert gateway_service["ports"] == [{"target": 8443, "published": 58080, "protocol": "tcp"}]
        assert gateway_service["networks"] == {"demo-shared": None}
        assert "environment" not in gateway_service
        livekit_override = spy.override_documents[2]["services"]["livekit"]
        assert livekit_override["networks"] == {"demo-shared": {"aliases": ["livekit-livekit"]}}
        assert livekit_override["ports"] == [{"published": 6178, "target": 6178}]
        content = spy.gateway["configs"]["boatyard-gateway-caddyfile"]["content"]
        assert "handle_path /auth/*" in content
        assert "header_up X-Forwarded-Proto http" in content
        assert "header_up X-Forwarded-Port 58080" in content
        assert "reverse_proxy placeframe-cuda-grafana:3000" in content
        assert content.rstrip().endswith("\thandle {\n\t\treverse_proxy placeframe-cuda-api:8000\n\t}\n}")

    def test_the_anonymous_edge_omits_the_auth_route(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path, bundles="placeframe-cuda")
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        gateway_service = spy.gateway["services"]["boatyard-gateway"]
        assert "environment" not in gateway_service
        content = spy.gateway["configs"]["boatyard-gateway-caddyfile"]["content"]
        assert "/auth/*" not in content

    def test_typed_port_overrides_win_over_the_config_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path, extra_values={"gateway_port": 18443, "ports": {"livekit": 16178}})
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.gateway["services"]["boatyard-gateway"]["ports"] == [
            {"target": 8443, "published": 18443, "protocol": "tcp"}
        ]
        assert spy.override_documents[2]["services"]["livekit"]["ports"] == [{"published": 16178, "target": 6178}]

    def test_an_unselected_port_declaring_entry_sits_inert(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "services": {
                    "placeframe": {
                        "options": {
                            "cuda": {
                                "workloads": ["api.yaml"],
                                "ports": [{"service": "api", "published": 16000, "target": 8000}],
                            },
                            "rocm": {
                                "workloads": ["api.yaml"],
                                "ports": [{"service": "api", "published": 17000, "target": 8000}],
                            },
                        }
                    }
                },
            },
        )
        _write_lock(tmp_path)
        _write_bundle_output(tmp_path, "placeframe-cuda", ["api"])
        _write_bundle_output(tmp_path, "placeframe-rocm", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_values_file(
            tmp_path,
            {"bundles": ["placeframe-rocm"], "ports": {"api": 19777}},
        )
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert "demo-placeframe-cuda" not in spy.projects
        assert spy.override_documents[0]["services"]["api"]["ports"] == [{"published": 19777, "target": 8000}]

    def test_the_gateway_renders_the_pinned_caddy_with_an_empty_image_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path)
        tmp_path.joinpath(*LOCK_PATH.parts).write_text(
            yaml.safe_dump({
                "platforms": ["linux/amd64"],
                "images": {},
                "bundles": {
                    "AUTH_KEYCLOAK_BUNDLE": {
                        "reference": KEYCLOAK_PINNED,
                        "services": ["keycloak"],
                        "routes": [],
                        "ports": [],
                        "container_names": [],
                    },
                    "LIVEKIT_BUNDLE": {
                        "reference": LIVEKIT_PINNED,
                        "services": ["livekit"],
                        "routes": [],
                        "ports": [],
                        "container_names": [],
                    },
                },
            }),
            encoding="utf-8",
        )
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.gateway["services"]["boatyard-gateway"]["image"] == "docker.io/library/caddy:2.8.4-alpine"

    def test_an_auth_selection_without_public_host_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path, bundles="placeframe-cuda,auth-keycloak", extra_values={"public_host": None})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "public_host" in str(result.exception)


class TestUpValidation:
    def _invoke_up(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        UpSpy().spy_up(monkeypatch)
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
        assert "'/grafana/*' references 'grafana'" in message

    def test_a_local_port_owner_absent_from_the_output_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_bundle_output(tmp_path, "placeframe-cuda", ["api"])
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "platforms": ["linux/amd64"],
                "gateway_port": 8080,
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
        _write_lock(tmp_path)
        _write_values_file(tmp_path, {"bundles": ["placeframe-cuda"]})

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "'3306' references 'grafana'" in str(result.exception)

    def test_a_local_output_defining_the_edge_service_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # A hand-edited generated output can spell the reserved name; the scan refuses it at bring-up.
        cuda = _write_consumer_tree(tmp_path)
        cuda.write_text(
            yaml.safe_dump({
                "services": {
                    "api": {"image": "demo/api:1"},
                    "grafana": {"image": "demo/grafana:1"},
                    "boatyard-gateway": {"image": "demo/edge:1"},
                }
            }),
            encoding="utf-8",
        )

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "reserved service 'boatyard-gateway'" in str(result.exception)

    def test_a_workload_slot_no_environment_source_fills_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path, api_environment={"TOKEN": "${API_TOKEN:?err}"})

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "placeframe-cuda requires ['API_TOKEN']" in str(result.exception)

    def test_a_lock_recorded_requirement_no_environment_source_fills_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_consumer_tree(tmp_path)
        _write_lock(
            tmp_path,
            bundles={
                "AUTH_KEYCLOAK_BUNDLE": KEYCLOAK_PINNED,
                "LIVEKIT_BUNDLE": {
                    "reference": LIVEKIT_PINNED,
                    "services": ["livekit"],
                    "slots": {"LIVEKIT_TOKEN": {"required": True}},
                },
            },
        )

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "livekit requires ['LIVEKIT_TOKEN']" in str(result.exception)

    def test_a_bare_workload_sha_slot_is_a_render_concern_not_a_demand(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The bare workload slot expands at render; the artifact carries the expansion, not a slot.
        _write_consumer_tree(
            tmp_path, api_environment={"VERSION": "${API_SHA}"}, bundle_environment={"VERSION": "tree-abc"}
        )

        result = self._invoke_up(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output

    def test_a_requirement_filled_by_the_env_file_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(
            tmp_path,
            extra_values={"values": {"LIVEKIT_TOKEN": "set"}},
        )
        _write_lock(
            tmp_path,
            bundles={
                "AUTH_KEYCLOAK_BUNDLE": KEYCLOAK_PINNED,
                "LIVEKIT_BUNDLE": {
                    "reference": LIVEKIT_PINNED,
                    "services": ["livekit"],
                    "slots": {"LIVEKIT_TOKEN": {"required": True}},
                },
            },
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
                "gateway_port": 8080,
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
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo-left-a", "demo-right-b", "demo"]
        assert spy.override_documents[0]["services"]["api"]["networks"] == {"demo-shared": {"aliases": ["left-a-api"]}}
        assert spy.override_documents[1]["services"]["api"]["networks"] == {"demo-shared": {"aliases": ["right-b-api"]}}
        content = spy.gateway["configs"]["boatyard-gateway-caddyfile"]["content"]
        assert "reverse_proxy left-a-api:8000" in content

    def test_the_same_overlap_stays_legal_at_down(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64"],
                "gateway_port": 8080,
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
        recorder = HandoffRecorder()
        monkeypatch.setattr(down, "bash", recorder.run)
        monkeypatch.setattr(down, "bash_check", _bash_check_true)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            f"docker compose -p demo-left-a --project-directory {tmp_path} down --remove-orphans",
            f"docker compose -p demo-right-b --project-directory {tmp_path} down --remove-orphans",
            f"docker compose -p demo --project-directory {tmp_path} down --remove-orphans",
            "docker network rm demo-shared",
        ]


class TestDownTeardown:
    def test_down_spells_file_less_per_project_invocations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        recorder = HandoffRecorder()
        monkeypatch.setattr(down, "bash", recorder.run)
        monkeypatch.setattr(down, "bash_check", _bash_check_true)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert len(recorder.commands) == 6
        for command in recorder.commands[:5]:
            assert command.startswith("docker compose -p ")
            assert " -f " not in command
            assert "--env-file" not in command
            assert command.endswith("down --remove-orphans")
        assert recorder.commands[-1] == "docker network rm demo-shared"

    def test_down_is_identical_under_every_selection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        commands: list[str] = []
        for bundles in ("placeframe-cuda,auth-keycloak", "", None):
            tree = tmp_path / f"tree-{len(commands)}"
            tree.mkdir()
            _write_consumer_tree(tree, bundles=bundles)
            recorder = HandoffRecorder()
            monkeypatch.setattr(down, "bash", recorder.run)
            monkeypatch.setattr(down, "bash_check", _bash_check_true)
            monkeypatch.chdir(tree)

            result = runner.invoke(app, ["down"])

            assert result.exit_code == 0, result.output
            normalized = _normalize(recorder.commands[0], tree)
            commands.append(normalized)
        assert commands[0] == commands[1] == commands[2]

    def test_down_forwards_the_volumes_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        recorder = HandoffRecorder()
        monkeypatch.setattr(down, "bash", recorder.run)
        monkeypatch.setattr(down, "bash_check", _bash_check_true)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down", "--volumes"])

        assert result.exit_code == 0, result.output
        assert len(recorder.commands) == 6
        assert all(command.endswith("down --remove-orphans -v") for command in recorder.commands[:5])

    def test_down_without_config_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_values_file(tmp_path, {"public_host": "example.dev"})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code != 0
        assert isinstance(result.exception, FileNotFoundError)

    def test_down_skips_the_network_removal_when_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(tmp_path)
        _write_values_file(tmp_path, {"public_host": "example.dev"})
        recorder = HandoffRecorder()
        monkeypatch.setattr(down, "bash", recorder.run)
        monkeypatch.setattr(down, "bash_check", _bash_check_false)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["down"])

        assert result.exit_code == 0, result.output
        assert recorder.commands == [
            f"docker compose -p demo-ext --project-directory {tmp_path} down --remove-orphans",
            f"docker compose -p demo --project-directory {tmp_path} down --remove-orphans",
        ]


class TestLockRequirement:
    def test_a_local_repo_without_a_lock_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "gateway_port": None,
                "platforms": ["linux/amd64"],
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_values_file(tmp_path, {"bundles": ["apps-core"]})
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(up, "bash_handoff", HandoffRecorder().run)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "records platforms [] but boatyard.yml declares ['linux/amd64']" in str(result.exception)


class TestLockEnvSynthesis:
    ALPINE_INDEX = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + FAKE_DIGEST
    ALPINE_MANIFEST = MIRROR_PREFIX + "/docker.io/library/alpine:3.20@" + PLATFORM_DIGEST

    def _invoke_up_with_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UpSpy:
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)
        return spy

    def test_the_attached_env_file_carries_the_platform_manifest_not_the_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "base_images": {"ALPINE_IMAGE": "docker.io/library/alpine:3.20"},
                "platforms": ["linux/amd64"],
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST})
        _write_values_file(tmp_path, {"bundles": ["apps-core"]})
        spy = self._invoke_up_with_lock(tmp_path, monkeypatch)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.env_content == (
            f"APPS_CORE__BOATYARD_LOCK__ALPINE_IMAGE={self.ALPINE_MANIFEST}\n" + BARRIER_MINT_LINE + EDGE_BASE_URL_LINE
        )

    def test_a_selected_local_entry_reads_its_lock_keys_under_the_entry_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "base_images": {"ALPINE_IMAGE": "docker.io/library/alpine:3.20"},
                "platforms": ["linux/amd64"],
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        _write_workload(tmp_path, "api.yaml", "api")
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST})
        _write_values_file(tmp_path, {"bundles": ["apps-core"]})
        spy = self._invoke_up_with_lock(tmp_path, monkeypatch)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.env_content == (
            f"APPS_CORE__BOATYARD_LOCK__ALPINE_IMAGE={self.ALPINE_MANIFEST}\n" + BARRIER_MINT_LINE + EDGE_BASE_URL_LINE
        )

    def test_a_selected_local_entry_carries_its_tree_tags_under_the_entry_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "repository": "ghcr.io/org/demo",
                "base_images": {"ALPINE_IMAGE": "docker.io/library/alpine:3.20"},
                "platforms": ["linux/amd64"],
                "services": {"apps": {"options": {"core": {"workloads": ["api.yaml"]}}}},
            },
        )
        _write_bundle_output(tmp_path, "apps-core", ["api"])
        api_image = tmp_path / "workloads" / "api"
        api_image.mkdir(parents=True)
        (api_image / "Dockerfile").write_text("ARG ALPINE_IMAGE\nFROM ${ALPINE_IMAGE}\n", encoding="utf-8")
        score = tmp_path / "stack" / "score"
        score.mkdir(parents=True, exist_ok=True)
        (score / "api.yaml").write_text(
            "metadata:\n"
            "  name: api\n"
            "  annotations:\n"
            "    boatyard/dockerfile: api=workloads/api/Dockerfile\n"
            "containers:\n"
            "  api: {}\n",
            encoding="utf-8",
        )
        _write_lock(tmp_path, {"ALPINE_IMAGE": self.ALPINE_INDEX}, {"linux/amd64": self.ALPINE_MANIFEST})
        _write_values_file(tmp_path, {"bundles": ["apps-core"]})
        monkeypatch.setattr(Image, "compute_sha", _producer_tree_sha)
        spy = self._invoke_up_with_lock(tmp_path, monkeypatch)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.env_content == (
            f"APPS_CORE__BOATYARD_LOCK__ALPINE_IMAGE={self.ALPINE_MANIFEST}\n"
            "APPS_CORE__BOATYARD_TREE__API_SHA=tree-consumer\n" + BARRIER_MINT_LINE + EDGE_BASE_URL_LINE
        )
        assert " -u APPS_CORE__BOATYARD_TREE__API_SHA " in spy.commands[0]

    def test_two_consecutive_syntheses_are_byte_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {})
        _write_lock(tmp_path, bundles=TUNNEL_BUNDLES)
        first_spy = self._invoke_up_with_lock(tmp_path, monkeypatch)

        first = runner.invoke(app, ["up"])
        assert first.exit_code == 0, first.output
        second_spy = self._invoke_up_with_lock(tmp_path, monkeypatch)
        second = runner.invoke(app, ["up"])

        assert second.exit_code == 0, second.output
        assert first_spy.env_content is not None and first_spy.env_content == second_spy.env_content

    def test_a_multi_platform_repo_selects_the_named_platform(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_config(
            tmp_path,
            {
                "project_name": "demo",
                "platforms": ["linux/amd64", "linux/arm64"],
                "repository": "ghcr.io/org/demo",
                "base_images": {"ALPINE_IMAGE": "docker.io/library/alpine:3.20"},
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
        spy = self._invoke_up_with_lock(tmp_path, monkeypatch)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.env_content == (
            f"APPS_CORE__BOATYARD_LOCK__ALPINE_IMAGE={arm64_manifest}\n" + BARRIER_MINT_LINE + EDGE_BASE_URL_LINE
        )

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
        _write_values_file(tmp_path, {})
        _write_lock(tmp_path, platform_entries={"linux/amd64": "", "linux/arm64": ""}, bundles=TUNNEL_BUNDLES)
        self._invoke_up_with_lock(tmp_path, monkeypatch)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "platform must be named in boatyard.values.yaml" in message
        assert "linux/amd64" in message and "linux/arm64" in message

    def test_an_undeclared_platform_selection_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {"platform": "linux/ppc64le"})
        _write_lock(tmp_path, bundles=TUNNEL_BUNDLES)
        self._invoke_up_with_lock(tmp_path, monkeypatch)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "linux/ppc64le" in message
        assert "linux/amd64" in message

    def test_a_failed_run_keeps_the_env_file_and_never_echoes_its_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {"values": {"SECRET_TOKEN": "supersecret"}})
        _write_lock(
            tmp_path,
            bundles={"TUNNEL_BUNDLE": {"reference": LIVEKIT_PINNED, "slots": {"SECRET_TOKEN": {"required": True}}}},
        )
        spy = UpSpy()
        spy.spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        def fail_first(command: str) -> None:
            spy.run(command)
            raise CalledProcessError(1, command)

        monkeypatch.setattr(up, "bash", fail_first)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit)
        report = str(result.exception)
        assert "the project demo-tunnel failed" in report
        retained = [Path(path) for path in report.split("kept artifacts for inspection: ", 1)[1].split(", ")]
        [env_file] = [path for path in retained if path.suffix == ".env"]
        assert env_file.read_text(encoding="utf-8") == (
            BARRIER_MINT_LINE + EDGE_BASE_URL_LINE + "SECRET_TOKEN=supersecret\n"
        )
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
        assert "boatyard.lock.yml lacks the TUNNEL_BUNDLE entry" in message

    def test_the_bundle_fetch_carries_the_locks_digest_not_the_yaml_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {})
        relocked = dict(TUNNEL_BUNDLES)
        relocked["TUNNEL_BUNDLE"] = f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{PLATFORM_DIGEST}"
        _write_lock(tmp_path, entries={}, bundles=relocked)
        spy = UpSpy(
            texts_by_reference={
                f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{PLATFORM_DIGEST}": [LIVEKIT_TEXT],
            }
        ).spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.artifacts.fetched == [f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{PLATFORM_DIGEST}"]


OVERRIDE_PATH = "../producer/stack/generated/compose/compose.placeframe-cuda.yaml"


class TestBundleOverrides:
    def _invoke_up(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> UpSpy:
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(root)
        return spy

    def test_the_path_form_replaces_the_registry_source_and_fills_from_the_producer_lock(
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
        spy = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo-placeframe-cuda", "demo"]
        assert output.resolve().as_posix() in spy.bundle_command("demo-placeframe-cuda")
        assert [_normalize(command, consumer, output) for command in spy.commands] == [
            (
                "env -u BOATYARD_BARRIER_IMAGE -u EDGE_BASE_URL -u PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE "
                "-u PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE "
                "docker compose -p demo-placeframe-cuda -f PRODUCER -f OVERRIDE "
                "--project-directory ROOT --env-file LOCKENV up --pull missing -d"
            ),
            (
                "env -u BOATYARD_BARRIER_IMAGE -u EDGE_BASE_URL -u PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE "
                "-u PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE "
                "docker compose -p demo -f GATEWAY --project-directory ROOT --env-file LOCKENV up --pull missing -d"
            ),
        ]
        assert spy.env_content == (
            BARRIER_MINT_LINE
            + EDGE_BASE_URL_LINE
            + (
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
            )
        )

    def test_the_producer_lock_fills_the_overridden_entrys_fan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64"]))
        _write_override_consumer(
            consumer,
            {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}},
            platforms=["linux/amd64"],
            lock_images={"LOKI_IMAGE": MIRROR_PREFIX + "/docker.io/library/loki:3.0@" + CONSUMER_ALPINE_DIGEST},
        )
        spy = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.env_content == (
            BARRIER_MINT_LINE
            + EDGE_BASE_URL_LINE
            + (
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
            )
        )

    def test_the_producer_tree_fills_the_overridden_entrys_fan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64"]), built=True)
        _write_override_consumer(
            consumer,
            {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}},
            platforms=["linux/amd64"],
        )
        monkeypatch.setattr(Image, "compute_sha", _producer_tree_sha)
        spy = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.env_content == (
            BARRIER_MINT_LINE
            + EDGE_BASE_URL_LINE
            + (
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
                "PLACEFRAME_CUDA__BOATYARD_TREE__API_SHA=tree-producer\n"
                "PLACEFRAME_CUDA__BOATYARD_TREE__GRAFANA_SHA=tree-producer\n"
            )
        )

    def test_the_producer_lock_selects_the_named_platform(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64", "linux/arm64"]))
        _write_override_consumer(
            consumer,
            {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}, "platform": "linux/arm64"},
            platforms=["linux/amd64", "linux/arm64"],
        )
        spy = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.env_content == (
            BARRIER_MINT_LINE
            + EDGE_BASE_URL_LINE
            + (
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_ARM64_DIGEST}\n"
            )
        )

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
        assert "the lock records ['linux/amd64', 'linux/arm64']" in message
        assert "platform must be named" in message

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
        assert "must be a filesystem path, not a URL" in str(result.exception)

    def test_an_unknown_override_name_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_override_consumer(
            consumer, {"path_overrides": {"ghost": "../producer/stack/generated/compose/compose.ghost.yaml"}}
        )
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "path_overrides: names ['ghost'] are not among the ref entries" in str(result.exception)

    def test_an_override_naming_a_local_entry_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        _write_override_consumer(
            consumer,
            {"path_overrides": {"extra-main": "../producer/stack/generated/compose/compose.extra.yaml"}},
            services_extra={"extra": {"options": {"main": {"workloads": ["extra.yaml"]}}}},
        )
        _write_workload(consumer, "extra.yaml", "extra")
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        message = str(result.exception)
        assert "path_overrides: names [\x27extra-main\x27]" in message
        assert "are not among the ref entries" in message

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
        spy = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo-placeframe-cuda", "demo"]
        assert output.resolve().as_posix() in spy.bundle_command("demo-placeframe-cuda")
        assert spy.env_content == (
            BARRIER_MINT_LINE
            + EDGE_BASE_URL_LINE
            + (
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__ALPINE_IMAGE={MIRROR_PREFIX}/docker.io/library/alpine:3.20@{PRODUCER_ALPINE_DIGEST}\n"
                f"PLACEFRAME_CUDA__BOATYARD_LOCK__LOKI_IMAGE={MIRROR_PREFIX}/docker.io/library/loki:3.0@{LOKI_AMD64_DIGEST}\n"
            )
        )

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
            yaml.safe_dump({
                "services": {"api": {"image": "demo/api:1"}, "boatyard-gateway": {"image": "demo/edge:1"}}
            }),
            encoding="utf-8",
        )
        _write_override_consumer(consumer, {"path_overrides": {"placeframe-cuda": OVERRIDE_PATH}})
        self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "reserved service 'boatyard-gateway'" in str(result.exception)

    def test_an_override_sharing_a_service_name_with_another_output_splits_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        producer = _write_producer(tmp_path, ["api", "grafana"], _producer_lock(["linux/amd64"]))
        _write_override_consumer(
            consumer,
            {
                "path_overrides": {"placeframe-cuda": OVERRIDE_PATH},
                "bundles": ["placeframe-cuda", "extra-main"],
            },
            services_extra={"extra": {"options": {"main": {"workloads": ["grafana.yaml"]}}}},
        )
        _write_workload(consumer, "grafana.yaml", "grafana")
        _write_bundle_output(consumer, "extra-main", ["api"])
        spy = self._invoke_up(monkeypatch, consumer)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["demo-placeframe-cuda", "demo-extra-main", "demo"]
        assert producer.resolve().as_posix() in spy.bundle_command("demo-placeframe-cuda")
        assert (consumer / "stack/generated/compose/compose.extra-main.yaml").resolve().as_posix() in (
            spy.bundle_command("demo-extra-main")
        )


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
        _write_consumer_tree(
            tmp_path,
            extra_values={"values": {"LIVEKIT_TOKEN": "set"}},
        )
        _write_lock(
            tmp_path,
            bundles={
                "AUTH_KEYCLOAK_BUNDLE": KEYCLOAK_PINNED,
                "LIVEKIT_BUNDLE": {
                    "reference": LIVEKIT_PINNED,
                    "services": ["livekit"],
                    "slots": {"LIVEKIT_TOKEN": {"required": True}},
                },
            },
        )
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert " -u LIVEKIT_TOKEN " in spy.commands[0]

    def test_the_scrub_defeats_a_shell_set_unfilled_optional(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_consumer_tree(tmp_path)
        _write_lock(
            tmp_path,
            bundles={
                "AUTH_KEYCLOAK_BUNDLE": KEYCLOAK_PINNED,
                "LIVEKIT_BUNDLE": {
                    "reference": LIVEKIT_PINNED,
                    "services": ["livekit"],
                    "slots": {"LIVEKIT_TOKEN": {"required": False}},
                },
            },
        )
        monkeypatch.setenv("LIVEKIT_TOKEN", "shell-value")
        spy = UpSpy().spy_up(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert " -u LIVEKIT_TOKEN " in spy.commands[0]
        assert spy.env_content is not None
        assert "LIVEKIT_TOKEN=\n" in spy.env_content

    def test_a_values_key_spelling_the_lock_marker_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        _write_values_file(
            tmp_path,
            {"values": {"TUNNEL__BOATYARD_LOCK__ALPINE_IMAGE": "docker.io/library/alpine:3.20"}},
        )
        _write_lock(tmp_path, bundles=TUNNEL_BUNDLES)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "must not carry a reserved __BOATYARD_LOCK__/" in str(result.exception)
        assert "key 'TUNNEL__BOATYARD_LOCK__ALPINE_IMAGE' must not carry" in str(result.exception)

    def test_a_values_key_spelling_the_tree_marker_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {"values": {"TUNNEL__BOATYARD_TREE__API_SHA": "tree-abc"}})
        _write_lock(tmp_path, bundles=TUNNEL_BUNDLES)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "must not carry a reserved __BOATYARD_LOCK__/" in str(result.exception)
        assert "key 'TUNNEL__BOATYARD_TREE__API_SHA' must not carry" in str(result.exception)

    def test_a_values_key_outside_the_env_name_grammar_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _write_ref_tree(tmp_path)
        _write_values_file(tmp_path, {"values": {"lowercase_key": "value"}})
        _write_lock(tmp_path, bundles=TUNNEL_BUNDLES)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "is not an ALL_CAPS environment name" in str(result.exception)


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
