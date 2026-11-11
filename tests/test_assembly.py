import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from bashrun.bash import CalledProcessError
from typer.testing import CliRunner

from boatyard.cli import app
from boatyard.models import values_file
from boatyard.models.config_file import ConfigFile
from boatyard.models.lock_file import LOCK_PATH, LockFile
from boatyard.verbs import down, up
from boatyard.resolve import resolve

FAKE_DIGEST = "sha256:" + "a" * 64
LIVEKIT_SHA_TAG = "a" * 40
LIVEKIT_REF = "oci://ghcr.io/org/livekit:" + LIVEKIT_SHA_TAG
LIVEKIT_PINNED = f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{FAKE_DIGEST}"
KEYCLOAK_REF = "oci://ghcr.io/org/keycloak:0.3.1"
KEYCLOAK_PINNED = f"ghcr.io/org/keycloak:0.3.1@{FAKE_DIGEST}"

LIVEKIT_TEXT = "services:\n  livekit:\n    image: demo/livekit:1\n"
KEYCLOAK_TEXT = "services:\n  keycloak:\n    image: demo/keycloak:1\n"

runner = CliRunner()


yaml.SafeLoader.add_constructor("!reset", lambda _loader, _node: None)


def _load_override(text: str) -> dict[str, Any]:
    return yaml.safe_load(text)


class StubArtifacts:
    def __init__(self, texts_by_reference: dict[str, list[str]]) -> None:
        self.texts_by_reference = texts_by_reference
        self.fetched: list[str] = []

    def fetch_compose_texts(self, pinned: str) -> list[str]:
        self.fetched.append(pinned)
        return self.texts_by_reference[pinned]


class UpSpy:
    # Captures the per-project invocations: command order, project names, the shared env file
    # content, the gateway document, and each bundle's override document.
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.projects: list[str] = []
        self.env_content: str | None = None
        self.gateway_documents: list[dict[str, Any]] = []
        self.override_documents: list[dict[str, Any]] = []

    def run(self, command: str) -> None:
        self.commands.append(command)
        self.projects.append(command.split(" -p ", 1)[1].split()[0])
        env_match = re.search(r"--env-file (\S+)", command)
        if env_match is not None and self.env_content is None:
            self.env_content = Path(env_match.group(1)).read_text(encoding="utf-8")
        gateway_match = re.search(r"-f (\S*boatyard-gateway-\S+\.yaml)", command)
        if gateway_match is not None:
            self.gateway_documents.append(yaml.safe_load(Path(gateway_match.group(1)).read_text(encoding="utf-8")))
        for override_match in re.finditer(r"-f (\S*boatyard-override-\S+\.yaml)", command):
            self.override_documents.append(_load_override(Path(override_match.group(1)).read_text(encoding="utf-8")))


def _bash_check_true(_command: str) -> bool:
    return True


def _write_edge_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routes: list[dict[str, object]],
    *,
    auth: bool = False,
    gateway_port: int | None = 58080,
    public_host: str | None = "192.168.8.10",
    public_scheme: str | None = None,
    public_port: int | None = 58080,
    values_extra: dict[str, object] | None = None,
    entry_ports: list[dict[str, object]] | None = None,
) -> None:
    services: dict[str, dict[str, object]] = {
        "livekit": {"ref": LIVEKIT_REF, "routes": routes},
    }
    if entry_ports is not None:
        services["livekit"]["ports"] = entry_ports
    if auth:
        services["auth"] = {
            "options": {
                "keycloak": {
                    "ref": KEYCLOAK_REF,
                    "routes": [{"path": "/auth/*", "upstream": "keycloak:8080", "strip_prefix": True}],
                }
            }
        }
        services = {"auth": services.pop("auth"), **services}
    payload: dict[str, object] = {
        "requires": ">=0.0",
        "project_name": "edge-demo",
        "platforms": ["linux/amd64"],
        "services": services,
    }
    if gateway_port is not None:
        payload["gateway_port"] = gateway_port
    (tmp_path / "boatyard.yml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    livekit_bundle: dict[str, object] = {
        "reference": LIVEKIT_PINNED,
        "services": ["livekit"],
        "routes": [],
        "ports": [],
        "container_names": [],
    }
    bundles: dict[str, object] = {"LIVEKIT_BUNDLE": livekit_bundle}
    if auth:
        bundles["AUTH_KEYCLOAK_BUNDLE"] = {
            "reference": KEYCLOAK_PINNED,
            "services": ["keycloak"],
            "routes": [],
            "ports": [],
            "container_names": [],
        }
    tmp_path.joinpath(*LOCK_PATH.parts).write_text(
        yaml.safe_dump({
            "platforms": ["linux/amd64"],
            "images": {},
            "bundles": bundles,
        }),
        encoding="utf-8",
    )
    values: dict[str, object] = {
        key: value
        for key, value in {
            "public_host": public_host,
            "public_scheme": public_scheme,
            "public_port": public_port,
        }.items()
        if value is not None
    }
    if auth:
        values["bundles"] = ["auth-keycloak"]
    values.update(values_extra or {})
    (tmp_path / "boatyard.values.yaml").write_text(
        yaml.safe_dump(values, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)


def _invoke_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routes: list[dict[str, object]],
    *,
    auth: bool = False,
    gateway_port: int | None = 58080,
    public_host: str | None = "192.168.8.10",
    public_scheme: str | None = None,
    public_port: int | None = 58080,
    values_extra: dict[str, object] | None = None,
    entry_ports: list[dict[str, object]] | None = None,
    livekit_text: str = LIVEKIT_TEXT,
) -> UpSpy:
    _write_edge_tree(
        tmp_path,
        monkeypatch,
        routes,
        auth=auth,
        gateway_port=gateway_port,
        public_host=public_host,
        public_scheme=public_scheme,
        public_port=public_port,
        values_extra=values_extra,
        entry_ports=entry_ports,
    )
    spy = UpSpy()
    monkeypatch.setattr(up, "bash", spy.run)
    monkeypatch.setattr(up, "bash_check", _bash_check_true)
    monkeypatch.setattr(up, "install_crane", lambda: None)
    monkeypatch.setattr(
        up,
        "OciArtifactManifest",
        StubArtifacts({
            LIVEKIT_PINNED: [livekit_text],
            KEYCLOAK_PINNED: [KEYCLOAK_TEXT],
        }),
    )
    return spy


def _up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routes: list[dict[str, object]],
    *,
    auth: bool = False,
    gateway_port: int | None = 58080,
    public_host: str | None = "192.168.8.10",
    public_scheme: str | None = None,
    public_port: int | None = 58080,
    values_extra: dict[str, object] | None = None,
    entry_ports: list[dict[str, object]] | None = None,
    livekit_text: str = LIVEKIT_TEXT,
) -> UpSpy:
    spy = _invoke_up(
        tmp_path,
        monkeypatch,
        routes,
        auth=auth,
        gateway_port=gateway_port,
        public_host=public_host,
        public_scheme=public_scheme,
        public_port=public_port,
        values_extra=values_extra,
        entry_ports=entry_ports,
        livekit_text=livekit_text,
    )
    result = runner.invoke(app, ["up"])
    assert result.exit_code == 0, result.output
    return spy


def _gateway_document(spy: UpSpy) -> dict[str, Any]:
    [document] = spy.gateway_documents
    return document


def _caddyfile(spy: UpSpy) -> str:
    return _gateway_document(spy)["configs"]["boatyard-gateway-caddyfile"]["content"]


def _resolve_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gateway_port: int | None = 58080,
    public_host: str | None = None,
    livekit_routes: list[dict[str, object]] | None = None,
    livekit_ports: list[dict[str, object]] | None = None,
    lock_livekit_routes: list[dict[str, object]] | None = None,
    lock_livekit_ports: list[dict[str, object]] | None = None,
    lock_livekit_slots: dict[str, object] | None = None,
    values_extra: dict[str, object] | None = None,
):
    routes = livekit_routes if livekit_routes is not None else []
    livekit_entry: dict[str, object] = {"ref": LIVEKIT_REF, "routes": routes}
    if livekit_ports is not None:
        livekit_entry["ports"] = livekit_ports
    payload: dict[str, object] = {
        "requires": ">=0.0",
        "project_name": "edge-demo",
        "platforms": ["linux/amd64"],
        "services": {"livekit": livekit_entry},
    }
    if gateway_port is not None:
        payload["gateway_port"] = gateway_port
    (tmp_path / "boatyard.yml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    livekit_bundle: dict[str, object] = {
        "reference": LIVEKIT_PINNED,
        "services": ["livekit"],
        "routes": lock_livekit_routes or [],
        "ports": lock_livekit_ports or [],
        "container_names": [],
    }
    if lock_livekit_slots is not None:
        livekit_bundle["slots"] = lock_livekit_slots
    lock_images: dict[str, object] = {}
    tmp_path.joinpath(*LOCK_PATH.parts).write_text(
        yaml.safe_dump({
            "platforms": ["linux/amd64"],
            "images": lock_images,
            "bundles": {"LIVEKIT_BUNDLE": livekit_bundle},
        }),
        encoding="utf-8",
    )
    values: dict[str, object] = dict(values_extra or {})
    if public_host is not None:
        values["public_host"] = public_host
    (tmp_path / "boatyard.values.yaml").write_text(
        yaml.safe_dump(values, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = ConfigFile.load(tmp_path / "boatyard.yml")
    lock = LockFile.load(tmp_path / LOCK_PATH, config)
    return resolve(config, lock, values_file.ValuesFile.load(tmp_path / "boatyard.values.yaml"))


class TestPublicFaceMinting:
    def test_the_four_keys_compose_the_slots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = _resolve_tree(
            tmp_path,
            monkeypatch,
            public_host="example.dev",
            values_extra={"public_scheme": "https", "public_port": 8443, "public_url_path": "/makeitsing"},
        )

        assert plan.gateway is not None and plan.gateway.public_face is not None
        assert plan.gateway.public_face == ("example.dev", "https", 8443, "/makeitsing")
        assert plan.env["PUBLIC_HOST"] == "example.dev"
        assert plan.env["PUBLIC_SCHEME"] == "https"
        assert plan.env["PUBLIC_PORT"] == "8443"
        assert plan.env["PUBLIC_URL_PATH"] == "/makeitsing"
        assert plan.env["PUBLIC_URL"] == "https://example.dev:8443/makeitsing"

    def test_the_scheme_default_port_elides_from_the_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = _resolve_tree(
            tmp_path, monkeypatch, gateway_port=None, public_host="example.dev", values_extra={"public_scheme": "https"}
        )

        assert plan.env["PUBLIC_PORT"] == "443"
        assert plan.env["PUBLIC_URL"] == "https://example.dev"

    def test_public_port_is_always_concrete(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = _resolve_tree(tmp_path, monkeypatch, gateway_port=None, public_host="example.dev")

        assert plan.gateway is None
        assert plan.env["PUBLIC_PORT"] == "80"
        assert plan.env["PUBLIC_URL"] == "http://example.dev"

    def test_the_minted_slots_refuse_as_hand_set_keys(self, tmp_path: Path) -> None:
        for name in ("PUBLIC_HOST", "PUBLIC_SCHEME", "PUBLIC_PORT", "PUBLIC_URL_PATH", "PUBLIC_URL", "EDGE_BASE_URL"):
            document = {"values": {name: "x"}}
            with pytest.raises(ValueError, match=f"{name} is not hand-settable"):
                values_file.ValuesFile.model_validate(document, context={"path": "boatyard.values.yaml"})

    def test_a_public_slot_consumption_without_public_host_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = tmp_path / "case"
        case.mkdir()
        with pytest.raises(ValueError, match="consumes PUBLIC_HOST, which requires public_host"):
            _resolve_tree(
                case,
                monkeypatch,
                public_host=None,
                lock_livekit_slots={"PUBLIC_HOST": {"required": True}},
            )

    def test_the_edge_base_url_mints_from_the_gateway_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve_tree(tmp_path, monkeypatch)

        assert plan.env["EDGE_BASE_URL"] == "http://boatyard-gateway:8443"


class TestGatewayProject:
    def test_the_gateway_service_carries_the_shared_net_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}])

        document = _gateway_document(spy)
        assert document["name"] == "edge-demo"
        assert document["networks"] == {"edge-demo-shared": {"external": True}}
        assert document["services"]["boatyard-gateway"] == {
            "image": "docker.io/library/caddy:2.8.4-alpine",
            "configs": [{"source": "boatyard-gateway-caddyfile", "target": "/etc/caddy/Caddyfile"}],
            "ports": [{"target": 8443, "published": 58080, "protocol": "tcp"}],
            "networks": {"edge-demo-shared": None},
        }

    def test_the_published_port_follows_the_values_gateway_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _up(
            tmp_path,
            monkeypatch,
            [{"path": "/*", "upstream": "livekit:8000"}],
            gateway_port=8080,
            public_port=8080,
            values_extra={"gateway_port": 18443},
        )

        assert _gateway_document(spy)["services"]["boatyard-gateway"]["ports"] == [
            {"target": 8443, "published": 18443, "protocol": "tcp"}
        ]

    def test_the_gateway_carries_no_environment_stanza(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], auth=True)

        assert "environment" not in _gateway_document(spy)["services"]["boatyard-gateway"]

    def test_routes_without_a_gateway_refuse_at_load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_edge_tree(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], gateway_port=None)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "routes require a gateway declaration" in str(result.exception)


class TestOverrideDocument:
    def test_each_service_joins_the_shared_network_under_its_entry_prefixed_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], auth=True)

        assert spy.override_documents == [
            {
                "networks": {"edge-demo-shared": {"external": True}},
                "services": {
                    "keycloak": {"networks": {"edge-demo-shared": {"aliases": ["auth-keycloak-keycloak"]}}},
                },
            },
            {
                "networks": {"edge-demo-shared": {"external": True}},
                "services": {
                    "livekit": {"networks": {"edge-demo-shared": {"aliases": ["livekit-livekit"]}}},
                },
            },
        ]

    def test_effective_ports_ride_except_what_the_bundle_text_declares(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ports: list[dict[str, object]] = [{"service": "livekit", "published": 6178, "target": 6178}]
        spy = _up(
            tmp_path,
            monkeypatch,
            [{"path": "/*", "upstream": "livekit:8000"}],
            entry_ports=ports,
            values_extra={"ports": {"livekit": 16178}},
        )
        declaring = "services:\n  livekit:\n    image: demo/livekit:1\n    ports:\n      - 6178:6178\n"
        (tmp_path / "declared").mkdir()
        declared_spy = _up(
            tmp_path / "declared",
            monkeypatch,
            [{"path": "/*", "upstream": "livekit:8000"}],
            entry_ports=ports,
            livekit_text=declaring,
        )

        assert spy.override_documents[0]["services"]["livekit"]["ports"] == [
            {"published": 16178, "target": 6178},
        ]
        assert "ports" not in declared_spy.override_documents[0]["services"]["livekit"]

    def test_container_name_resets_only_where_the_bundle_declares_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_edge_tree(tmp_path, monkeypatch, [])
        lock = yaml.safe_load((tmp_path / LOCK_PATH).read_text(encoding="utf-8"))
        lock["bundles"]["LIVEKIT_BUNDLE"]["container_names"] = ["livekit"]
        (tmp_path / LOCK_PATH).write_text(yaml.safe_dump(lock), encoding="utf-8")
        spy = UpSpy()
        monkeypatch.setattr(up, "bash", spy.run)
        monkeypatch.setattr(up, "bash_check", _bash_check_true)
        monkeypatch.setattr(up, "install_crane", lambda: None)
        monkeypatch.setattr(up, "OciArtifactManifest", StubArtifacts({LIVEKIT_PINNED: [LIVEKIT_TEXT]}))

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        [override] = spy.override_documents
        assert override["services"]["livekit"]["container_name"] is None


class TestUpProjects:
    def test_bundles_then_gateway_in_declaration_order(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], auth=True)

        assert spy.projects == ["edge-demo-auth-keycloak", "edge-demo-livekit", "edge-demo"]

    def test_every_bundle_invocation_carries_its_compose_files_and_the_shared_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}])

        [bundle_command, gateway_command] = spy.commands
        assert bundle_command.startswith(
            "env -u BOATYARD_BARRIER_IMAGE -u EDGE_BASE_URL -u PUBLIC_HOST -u PUBLIC_PORT -u PUBLIC_SCHEME "
            "-u PUBLIC_URL -u PUBLIC_URL_PATH docker compose -p edge-demo-livekit -f "
        )
        assert "boatyard-bundle-" in bundle_command
        assert "boatyard-override-" in bundle_command
        assert bundle_command.endswith("up --pull missing -d")
        assert gateway_command.startswith(
            "env -u BOATYARD_BARRIER_IMAGE -u EDGE_BASE_URL -u PUBLIC_HOST -u PUBLIC_PORT -u PUBLIC_SCHEME "
            "-u PUBLIC_URL -u PUBLIC_URL_PATH docker compose -p edge-demo -f "
        )
        assert "boatyard-gateway-" in gateway_command
        assert spy.env_content is not None and spy.env_content.startswith(
            "BOATYARD_BARRIER_IMAGE=docker.io/library/alpine:3.20\nEDGE_BASE_URL="
        )

    def test_a_failing_project_stops_the_run_and_reports_the_retained_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _invoke_up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}])

        def fail_from_second(command: str) -> None:
            spy.run(command)
            if len(spy.commands) == 2:
                raise CalledProcessError(1, command)

        monkeypatch.setattr(up, "bash", fail_from_second)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit)
        report = str(result.exception)
        assert "the project edge-demo failed" in report
        assert "kept artifacts for inspection:" in report

    def test_zero_selection_renders_the_gateway_project_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "requires": ">=0.0",
            "project_name": "edge-demo",
            "platforms": ["linux/amd64"],
            "gateway_port": 58080,
            "services": {
                "core": {
                    "options": {
                        "cuda": {
                            "ref": LIVEKIT_REF,
                            "routes": [{"path": "/*", "upstream": "livekit:8000"}],
                        }
                    }
                }
            },
        }
        (tmp_path / "boatyard.yml").write_text(
            yaml.safe_dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8"
        )
        tmp_path.joinpath(*LOCK_PATH.parts).write_text(
            yaml.safe_dump({
                "platforms": ["linux/amd64"],
                "images": {},
                "bundles": {
                    "CORE_CUDA_BUNDLE": {
                        "reference": LIVEKIT_PINNED,
                        "services": ["livekit"],
                        "routes": [],
                        "ports": [],
                        "container_names": [],
                    }
                },
            }),
            encoding="utf-8",
        )
        (tmp_path / "boatyard.values.yaml").write_text("public_host: 192.168.8.10\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        spy = UpSpy()
        monkeypatch.setattr(up, "bash", spy.run)
        monkeypatch.setattr(up, "bash_check", _bash_check_true)
        monkeypatch.setattr(up, "install_crane", lambda: None)
        monkeypatch.setattr(up, "OciArtifactManifest", StubArtifacts({LIVEKIT_PINNED: [LIVEKIT_TEXT]}))

        result = runner.invoke(app, ["up"])

        assert result.exit_code == 0, result.output
        assert spy.projects == ["edge-demo"]
        assert _caddyfile(spy) == ":8443 {\n}\n"


class TestDownLoop:
    def _invoke_down(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *arguments: str) -> list[str]:
        _write_edge_tree(tmp_path, monkeypatch, [], auth=True)
        commands: list[str] = []
        monkeypatch.setattr(down, "bash", commands.append)
        monkeypatch.setattr(down, "bash_check", _bash_check_true)

        result = runner.invoke(app, ["down", *arguments])

        assert result.exit_code == 0, result.output
        return commands

    def test_the_loop_derives_from_the_configs_full_entry_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        commands = self._invoke_down(tmp_path, monkeypatch)

        assert commands == [
            f"docker compose -p edge-demo-auth-keycloak --project-directory {tmp_path} down --remove-orphans",
            f"docker compose -p edge-demo-livekit --project-directory {tmp_path} down --remove-orphans",
            f"docker compose -p edge-demo --project-directory {tmp_path} down --remove-orphans",
            "docker network rm edge-demo-shared",
        ]

    def test_the_volumes_flag_rides_every_invocation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        commands = self._invoke_down(tmp_path, monkeypatch, "--volumes")

        assert all(command.endswith("down --remove-orphans -v") for command in commands[:3])
        assert commands[-1] == "docker network rm edge-demo-shared"

    def test_the_network_removal_trails_the_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        commands = self._invoke_down(tmp_path, monkeypatch)

        assert commands[-1] == "docker network rm edge-demo-shared"
        assert all(command.startswith("docker compose") for command in commands[:-1])


class TestRenderCaddyfile:
    def test_the_site_listens_on_the_edge_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}])

        assert _caddyfile(spy).startswith(":8443 {\n")

    def test_the_catch_all_renders_as_a_bare_handle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}])

        assert "\thandle {\n\t\treverse_proxy livekit-livekit:8000\n\t}\n}" in _caddyfile(spy)

    def test_wildcard_paths_render_as_handle_matchers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/grafana/*", "upstream": "grafana:3000"}])

        assert "\thandle /grafana/* {\n\t\treverse_proxy livekit-grafana:3000\n\t}" in _caddyfile(spy)

    def test_strip_prefix_renders_as_handle_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/grafana/*", "upstream": "grafana:3000", "strip_prefix": True}])

        assert "\thandle_path /grafana/* {\n\t\treverse_proxy livekit-grafana:3000\n\t}" in _caddyfile(spy)

    def test_the_auth_route_bakes_the_forwarded_literals(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], auth=True)

        caddyfile = _caddyfile(spy)
        assert "reverse_proxy auth-keycloak-keycloak:8080 {" in caddyfile
        assert "header_up X-Forwarded-Proto http" in caddyfile
        assert "header_up X-Forwarded-Port 58080" in caddyfile

    def test_plain_routes_carry_no_forwarded_headers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], auth=True)

        assert "header_up" not in _caddyfile(spy).split("handle {", 1)[1]

    def test_a_raw_route_renders_verbatim_under_its_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(
            tmp_path,
            monkeypatch,
            [{"path": "/x/*", "upstream": "x:1", "raw": 'respond "raw" 200'}],
        )

        assert '\thandle /x/* {\n\t\trespond "raw" 200\n\t}' in _caddyfile(spy)

    def test_blocks_render_in_declaration_order(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _up(
            tmp_path,
            monkeypatch,
            [
                {"path": "/*", "upstream": "livekit:8000"},
                {"path": "/grafana/*", "upstream": "grafana:3000"},
            ],
        )

        caddyfile = _caddyfile(spy)
        assert caddyfile.index("reverse_proxy livekit-livekit:8000") < caddyfile.index(
            "reverse_proxy livekit-grafana:3000"
        )


class TestAdoption:
    def test_authored_declarations_replace_the_adopted_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve_tree(
            tmp_path,
            monkeypatch,
            livekit_routes=[{"path": "/*", "upstream": "livekit:8000"}],
            lock_livekit_routes=[{"path": "/adopted/*", "upstream": "livekit:9000"}],
        )

        [bundle] = plan.bundles
        assert [(route.path, route.upstream) for route in bundle.effective_routes] == [("/*", "livekit:8000")]

    def test_the_adopted_set_stands_without_authored_declarations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _resolve_tree(
            tmp_path,
            monkeypatch,
            livekit_routes=[],
            lock_livekit_routes=[{"path": "/adopted/*", "upstream": "livekit:9000"}],
        )

        [bundle] = plan.bundles
        assert [(route.path, route.upstream) for route in bundle.effective_routes] == [("/adopted/*", "livekit:9000")]

    def test_two_adopted_claims_on_one_path_refuse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tree = tmp_path / "case"
        tree.mkdir()
        with pytest.raises(ValueError, match="route path /x/\\* is claimed by both"):
            _write_two_claim_tree(tree, monkeypatch)

    def test_the_values_ports_single_port_rule(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = _resolve_tree(
            tmp_path,
            monkeypatch,
            livekit_ports=[{"service": "livekit", "published": 6178, "target": 6178}],
            values_extra={"ports": {"livekit": 16178}},
        )

        [bundle] = plan.bundles
        assert [(port.service, port.published) for port in bundle.effective_ports] == [("livekit", 6178)]

    def test_an_unknown_values_ports_service_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="are services of no selected bundle"):
            _resolve_tree(
                tmp_path,
                monkeypatch,
                livekit_ports=[{"service": "livekit", "published": 6178, "target": 6178}],
                values_extra={"ports": {"ghost": 1234}},
            )


def _write_two_claim_tree(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "requires": ">=0.0",
        "project_name": "edge-demo",
        "platforms": ["linux/amd64"],
        "gateway_port": 58080,
        "services": {
            "one": {"ref": LIVEKIT_REF},
            "two": {"ref": KEYCLOAK_REF},
        },
    }
    (tree / "boatyard.yml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    (tree / LOCK_PATH).write_text(
        yaml.safe_dump({
            "platforms": ["linux/amd64"],
            "images": {},
            "bundles": {
                "ONE_BUNDLE": {
                    "reference": LIVEKIT_PINNED,
                    "services": ["one-a"],
                    "routes": [{"path": "/x/*", "upstream": "one-a:8000"}],
                    "ports": [],
                    "container_names": [],
                },
                "TWO_BUNDLE": {
                    "reference": KEYCLOAK_PINNED,
                    "services": ["two-b"],
                    "routes": [{"path": "/x/*", "upstream": "two-b:8000"}],
                    "ports": [],
                    "container_names": [],
                },
            },
        }),
        encoding="utf-8",
    )
    (tree / "boatyard.values.yaml").write_text("public_host: 192.168.8.10\n", encoding="utf-8")
    monkeypatch.chdir(tree)
    config = ConfigFile.load(tree / "boatyard.yml")
    lock = LockFile.load(tree / LOCK_PATH, config)
    resolve(config, lock, values_file.ValuesFile.load(tree / "boatyard.values.yaml"))


class TestValuesLoad:
    def test_folds_the_selection_and_validates_sections(self, tmp_path: Path) -> None:
        (tmp_path / "boatyard.yml").write_text(
            yaml.safe_dump(
                {
                    "requires": ">=0.0",
                    "project_name": "env-demo",
                    "services": {
                        "core": {"options": {"cuda": {"ref": "oci://ghcr.io/org/core:0.1.0"}}},
                        "ui": {"options": {"open": {"ref": "oci://ghcr.io/org/ui:0.1.0"}}},
                    },
                },
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        tmp_path.joinpath(*LOCK_PATH.parts).write_text(
            yaml.safe_dump(
                {
                    "bundles": {
                        "CORE_CUDA_BUNDLE": {
                            "reference": f"ghcr.io/org/core:0.1.0@{FAKE_DIGEST}",
                            "slots": {"RELOAD": {"required": False}, "GPU_MEM": {"required": True}},
                            "services": ["core"],
                            "routes": [],
                            "ports": [],
                            "container_names": [],
                        },
                        "UI_OPEN_BUNDLE": {
                            "reference": f"ghcr.io/org/ui:0.1.0@{FAKE_DIGEST}",
                            "services": ["ui"],
                            "routes": [],
                            "ports": [],
                            "container_names": [],
                        },
                    }
                },
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (tmp_path / "boatyard.values.yaml").write_text(
            yaml.safe_dump(
                {
                    "bundles": ["core-cuda", "ui-open"],
                    "values": {"RELOAD": True, "GPU_MEM": 512},
                },
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        config = ConfigFile.load(tmp_path / "boatyard.yml")
        lock = LockFile.load(tmp_path / "boatyard.lock.yml", config)
        parsed = values_file.ValuesFile.load(tmp_path / "boatyard.values.yaml")

        assert [bundle.entry_name for bundle in resolve(config, lock, parsed).bundles] == ["core-cuda", "ui-open"]
        assert parsed.values == {"RELOAD": "true", "GPU_MEM": "512"}
