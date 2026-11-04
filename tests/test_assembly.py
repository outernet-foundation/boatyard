import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from boatyard.cli import app
from boatyard import environment
from boatyard.verbs import up
from boatyard.config_lock import LOCK_PATH

FAKE_DIGEST = "sha256:" + "a" * 64
LIVEKIT_SHA_TAG = "a" * 40
LIVEKIT_REF = "oci://ghcr.io/org/livekit:" + LIVEKIT_SHA_TAG
KEYCLOAK_REF = "oci://ghcr.io/org/keycloak:0.3.1"
CADDY_LOCK_ENTRY = "ghcr.io/outernet-foundation/mirror/docker.io/library/caddy:2.8.4-alpine@" + FAKE_DIGEST

ASSEMBLY_ARGUMENT_PATTERN = re.compile(r"-f (\S*devkit-assembly-\S+\.yaml)")

runner = CliRunner()


class AssemblyCapture:
    def __init__(self) -> None:
        self.document: dict[str, Any] | None = None

    def run(self, command: str) -> None:
        match = ASSEMBLY_ARGUMENT_PATTERN.search(command)
        if match is not None:
            self.document = yaml.safe_load(Path(match.group(1)).read_text(encoding="utf-8"))


def _write_edge_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routes: list[dict[str, object]],
    *,
    auth: bool = False,
    gateway: str | None = "${GATEWAY_PORT:?err}",
    public_url: str = "http://192.168.8.10:58080",
) -> None:
    services: dict[str, object] = {
        "livekit": {"ref": LIVEKIT_REF, "routes": routes},
    }
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
    if gateway is not None:
        payload["gateway"] = gateway
    (tmp_path / "boatyard.yml").write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    tmp_path.joinpath(*LOCK_PATH.parts).write_text(
        yaml.safe_dump({
            "images": {
                "CADDY_IMAGE": {
                    "index": CADDY_LOCK_ENTRY,
                    "platforms": {"linux/amd64": CADDY_LOCK_ENTRY},
                }
            },
            "bundles": {
                "LIVEKIT_BUNDLE": {"reference": f"ghcr.io/org/livekit:{LIVEKIT_SHA_TAG}@{FAKE_DIGEST}"},
                "AUTH_KEYCLOAK_BUNDLE": {"reference": f"ghcr.io/org/keycloak:0.3.1@{FAKE_DIGEST}"},
            },
        }),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        f"GATEWAY_PORT=58080\nPUBLIC_URL={public_url}\nBUNDLES={'auth-keycloak' if auth else ''}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BUNDLES", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("GATEWAY_PORT", raising=False)
    monkeypatch.chdir(tmp_path)


def _assembly_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routes: list[dict[str, object]],
    *,
    auth: bool = False,
    gateway: str | None = "${GATEWAY_PORT:?err}",
    public_url: str = "http://192.168.8.10:58080",
) -> dict[str, Any]:
    _write_edge_tree(tmp_path, monkeypatch, routes, auth=auth, gateway=gateway, public_url=public_url)
    capture = AssemblyCapture()
    monkeypatch.setattr(up, "bash_handoff", capture.run)

    result = runner.invoke(app, ["up"])

    assert result.exit_code == 0, result.output
    assert capture.document is not None
    return capture.document


def _caddyfile(document: dict[str, Any]) -> str:
    return document["configs"]["devkit-edge-caddyfile"]["content"]


class TestRenderCaddyfile:
    def test_the_site_listens_on_the_edge_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        document = _assembly_document(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}])

        assert _caddyfile(document).startswith(":8443 {\n")

    def test_the_catch_all_renders_as_a_bare_handle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        document = _assembly_document(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}])

        assert "\thandle {\n\t\treverse_proxy livekit-livekit:8000\n\t}\n}" in _caddyfile(document)

    def test_wildcard_paths_render_as_handle_matchers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        document = _assembly_document(tmp_path, monkeypatch, [{"path": "/grafana/*", "upstream": "grafana:3000"}])

        assert "\thandle /grafana/* {\n\t\treverse_proxy livekit-grafana:3000\n\t}" in _caddyfile(document)

    def test_strip_prefix_renders_as_handle_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        document = _assembly_document(
            tmp_path, monkeypatch, [{"path": "/grafana/*", "upstream": "grafana:3000", "strip_prefix": True}]
        )

        assert "\thandle_path /grafana/* {\n\t\treverse_proxy livekit-grafana:3000\n\t}" in _caddyfile(document)

    def test_the_auth_route_carries_the_forwarded_headers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = _assembly_document(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], auth=True)

        caddyfile = _caddyfile(document)
        assert "\t\t\treverse_proxy auth-keycloak-keycloak:8080 {" in caddyfile.replace(
            "\t\treverse_proxy", "\t\t\treverse_proxy"
        )
        assert "header_up X-Forwarded-Proto {$$FORWARDED_PROTO}" in caddyfile
        assert "header_up X-Forwarded-Port {$$FORWARDED_PORT}" in caddyfile

    def test_plain_routes_carry_no_forwarded_headers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        document = _assembly_document(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], auth=True)

        assert "header_up" not in _caddyfile(document).split("handle {", 1)[1]

    def test_a_raw_route_renders_verbatim_under_its_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        document = _assembly_document(
            tmp_path,
            monkeypatch,
            [{"path": "/x/*", "upstream": "x:1", "raw": 'respond "raw" 200'}],
        )

        assert '\thandle /x/* {\n\t\trespond "raw" 200\n\t}' in _caddyfile(document)

    def test_blocks_render_in_declaration_order(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        document = _assembly_document(
            tmp_path,
            monkeypatch,
            [
                {"path": "/*", "upstream": "livekit:8000"},
                {"path": "/grafana/*", "upstream": "grafana:3000"},
            ],
        )

        caddyfile = _caddyfile(document)
        assert caddyfile.index("reverse_proxy livekit-livekit:8000") < caddyfile.index(
            "reverse_proxy livekit-grafana:3000"
        )


class TestPublicUrlDerivation:
    @pytest.mark.parametrize(
        ("public_url", "expected"),
        [
            ("http://192.168.8.10:58080", ("http", "58080")),
            ("https://example.dev:8443/path", ("https", "8443")),
            ("https://example.dev", ("https", "443")),
            ("http://example.dev", ("http", "80")),
        ],
    )
    def test_the_forwarded_pair_derives_from_public_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        public_url: str,
        expected: tuple[str, str],
    ) -> None:
        document = _assembly_document(
            tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], auth=True, public_url=public_url
        )

        assert document["services"]["edge"]["environment"] == {
            "FORWARDED_PROTO": expected[0],
            "FORWARDED_PORT": expected[1],
        }


class TestEdgeStanza:
    def test_the_stanza_carries_the_caddy_hole_config_mount_and_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = _assembly_document(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}])

        assert document["services"]["edge"] == {
            "image": "${CADDY_IMAGE:?err}",
            "configs": [{"source": "devkit-edge-caddyfile", "target": "/etc/caddy/Caddyfile"}],
            "ports": [{"target": "8443", "published": "${GATEWAY_PORT:?err}", "protocol": "tcp"}],
            "networks": {"livekit-net": None},
        }

    def test_the_published_port_uses_a_literal_gateway(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        document = _assembly_document(
            tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], gateway="8080"
        )

        assert document["services"]["edge"]["ports"] == [{"target": "8443", "published": "8080", "protocol": "tcp"}]

    def test_routes_without_a_gateway_refuse_at_load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_edge_tree(tmp_path, monkeypatch, [{"path": "/*", "upstream": "livekit:8000"}], gateway=None)

        result = runner.invoke(app, ["up"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "routes are declared; gateway is required" in str(result.exception)


class TestEnvironmentLoad:
    def test_parses_assignments_skipping_comments_and_blanks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text(
            "# comment\n\nBUNDLES=core-cuda,gateway-open\nGATEWAY_PORT='8443'\nRELOAD=\"true\"\nnot-an-assignment\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(environment, "environ", {})

        parsed = environment.Environment.load(tmp_path)

        assert parsed.bundles == ["core-cuda", "gateway-open"]
        assert parsed.variables == {"GATEWAY_PORT": "8443", "RELOAD": "true"}
