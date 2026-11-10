from __future__ import annotations

from pathlib import Path

import yaml

from boatyard.models.config_file import ConfigFile
from boatyard.models.lock_file import LOCK_PATH, LockFile
from boatyard.models.values_file import ValuesFile
from boatyard.paths import VALUES_FILE
from boatyard.resolve import AssemblyPlan, resolve

FAKE_DIGEST = "sha256:" + "a" * 64
MONITOR_PINNED = f"ghcr.io/org/monitor:1.0.0@{FAKE_DIGEST}"

WORKLOAD = """\
metadata:
  name: api
  annotations:
    outernet.internal/consumer-interpolated: /etc/rathole/client.toml
    outernet.internal/lock: api=LOKI_IMAGE
containers:
  api:
    environment:
      TOKEN: ${API_TOKEN:?err}
    files:
      /etc/rathole/client.toml:
        content: |
          remote_addr = "$${BIND_TOKEN:?err}"
resources:
  domain:
    type: example-domain
"""

PROVISIONER = """\
- uri: template://example/service-discovery
  type: example-domain
  services:
    extra-api:
      image: demo/extra:1
      environment:
        PROV_TOKEN: ${PROV_TOKEN:?err}
"""

# The bundle artifact generate-score renders from the workload and provisioner above: bare
# slots are expanded at render, suffixed slots remain as compose interpolation, lock keys remain
# under the entry marker, and consumer-interpolated content lands un-doubled.
BUNDLE = (
    "services:\n"
    "  api:\n"
    f"    image: ghcr.io/org/loki:3.0@{FAKE_DIGEST}\n"
    "    environment:\n"
    "      TOKEN: ${API_TOKEN:?err}\n"
    "  extra-api:\n"
    "    image: demo/extra:1\n"
    "    environment:\n"
    "      PROV_TOKEN: ${PROV_TOKEN:?err}\n"
    "configs:\n"
    "  client.toml:\n"
    "    content: |\n"
    '      remote_addr = "${BIND_TOKEN:?err}"\n'
)


def _monitor_slots(overrides: dict[str, object] | None) -> dict[str, object]:
    slots: dict[str, object] = {"MON_TOKEN": {"required": True}}
    if overrides is not None:
        slots = overrides
    return slots


def _write_repo(
    tmp_path: Path,
    values: dict[str, object],
    *,
    slots: dict[str, object] | None = None,
    bundle: str = BUNDLE,
    gateway: int | None = None,
    ports: list[dict[str, object]] | None = None,
) -> Path:
    root = tmp_path / "consumer"
    root.mkdir()
    score = root / "stack" / "score"
    score.mkdir(parents=True)
    (score / "api.yaml").write_text(WORKLOAD, encoding="utf-8")
    (score / "compose").mkdir()
    (score / "compose" / "services.yaml").write_text(PROVISIONER, encoding="utf-8")
    bundle_output = root / "stack" / "generated" / "compose" / "compose.core-main.yaml"
    bundle_output.parent.mkdir(parents=True)
    bundle_output.write_text(bundle, encoding="utf-8")
    base_images: dict[str, str] = {"LOKI_IMAGE": "docker.io/grafana/loki:3.0"}
    core_entry: dict[str, object] = {"options": {"main": {"workloads": ["api.yaml"]}}}
    if ports is not None:
        core_entry["ports"] = ports
    config: dict[str, object] = {
        "requires": ">=0",
        "project_name": "demo",
        "repository": "ghcr.io/org/demo",
        "platforms": ["linux/amd64"],
        "base_images": base_images,
        "services": {
            "core": core_entry,
            "monitor": {"ref": "oci://ghcr.io/org/monitor:1.0.0"},
        },
    }
    if gateway is not None:
        config["gateway_port"] = gateway
    (root / "boatyard.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    lock_images: dict[str, object] = {
        "LOKI_IMAGE": {
            "index": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}",
            "platforms": {"linux/amd64": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}"},
        },
        "ALPINE_IMAGE": {
            "index": f"ghcr.io/org/alpine:3.20@{FAKE_DIGEST}",
            "platforms": {"linux/amd64": f"ghcr.io/org/alpine:3.20@{FAKE_DIGEST}"},
        },
    }
    if gateway is not None:
        lock_images["CADDY_IMAGE"] = {
            "index": f"ghcr.io/org/caddy:2.8@{FAKE_DIGEST}",
            "platforms": {"linux/amd64": f"ghcr.io/org/caddy:2.8@{FAKE_DIGEST}"},
        }
    root.joinpath(*LOCK_PATH.parts).write_text(
        yaml.safe_dump({
            "platforms": ["linux/amd64"],
            "images": lock_images,
            "bundles": {
                "MONITOR_BUNDLE": {
                    "reference": MONITOR_PINNED,
                    "slots": _monitor_slots(slots),
                    "services": ["monitor"],
                    "routes": [],
                    "ports": [],
                    "container_names": [],
                }
            },
        }),
        encoding="utf-8",
    )
    (root / VALUES_FILE).write_text(yaml.safe_dump(values), encoding="utf-8")
    return root


def _filled_values(extra: dict[str, object] | None = None, **core_overrides: str) -> dict[str, object]:
    hand: dict[str, str] = {"API_TOKEN": "one", "BIND_TOKEN": "two", "PROV_TOKEN": "three", "MON_TOKEN": "four"}
    hand.update(core_overrides)
    values: dict[str, object] = {"bundles": ["core-main"], "values": hand}
    if extra is not None:
        values.update(extra)
    return values


def _core_values(**overrides: str) -> dict[str, str]:
    hand: dict[str, str] = {"API_TOKEN": "one", "BIND_TOKEN": "two", "PROV_TOKEN": "three", "MON_TOKEN": "four"}
    hand.update(overrides)
    return hand


def _load(root: Path) -> ValuesFile:
    return ValuesFile.load(root / VALUES_FILE)


def _resolve(root: Path) -> AssemblyPlan:
    config = ConfigFile.load(root / "boatyard.yml")
    lock = LockFile.load(root / LOCK_PATH, config)
    return resolve(config, lock, ValuesFile.load(root / VALUES_FILE))


def _refusal(
    tmp_path: Path,
    values: dict[str, object],
    *,
    slots: dict[str, object] | None = None,
    bundle: str = BUNDLE,
    gateway: int | None = None,
    ports: list[dict[str, object]] | None = None,
) -> str:
    try:
        _resolve(_write_repo(tmp_path, values, slots=slots, bundle=bundle, gateway=gateway, ports=ports))
    except ValueError as error:
        return str(error)
    raise AssertionError("expected a refusal")


def test_a_filled_values_file_loads_and_resolves(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, _filled_values(), slots={"MON_TOKEN": {"required": True}})

    assert _load(root).values == {
        "API_TOKEN": "one",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }
    assert [bundle.entry_name for bundle in _resolve(root).bundles] == ["core-main", "monitor"]


def test_a_remote_winners_records_come_from_the_lock(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, _filled_values(), gateway=9443)
    lock = yaml.safe_load((root / LOCK_PATH).read_text(encoding="utf-8"))
    assert isinstance(lock, dict)
    lock["bundles"]["MONITOR_BUNDLE"]["routes"] = [{"path": "/monitor/*", "upstream": "monitor:9000"}]
    lock["bundles"]["MONITOR_BUNDLE"]["ports"] = [{"service": "monitor", "published": 9000, "target": 9000}]
    (root / LOCK_PATH).write_text(yaml.safe_dump(lock), encoding="utf-8")

    monitor = next(bundle for bundle in _resolve(root).bundles if bundle.entry_name == "monitor")

    assert monitor.pinned_reference == MONITOR_PINNED
    assert monitor.bundle_path is None
    assert monitor.services == ["monitor"]
    assert [(route.path, route.upstream) for route in monitor.effective_routes] == [("/monitor/*", "monitor:9000")]
    assert [(port.service, port.published) for port in monitor.effective_ports] == [("monitor", 9000)]


def test_authored_declarations_replace_the_adopted_set_wholesale(tmp_path: Path) -> None:
    adopting = BUNDLE.replace(
        "      TOKEN: ${API_TOKEN:?err}\n",
        "      TOKEN: ${API_TOKEN:?err}\n    labels:\n      outernet.internal/publish: 7000:7000\n",
    )
    authored = [{"service": "api", "published": 5678, "target": 5678}]

    (tmp_path / "adopted").mkdir()
    (tmp_path / "authored").mkdir()
    adopted_root = _write_repo(tmp_path / "adopted", _filled_values(), bundle=adopting)
    authored_root = _write_repo(tmp_path / "authored", _filled_values(), bundle=adopting, ports=authored)

    adopted = next(bundle for bundle in _resolve(adopted_root).bundles if bundle.entry_name == "core-main")
    authored_bundle = next(bundle for bundle in _resolve(authored_root).bundles if bundle.entry_name == "core-main")

    assert [(port.service, port.published) for port in adopted.effective_ports] == [("api", 7000)]
    assert [(port.service, port.published) for port in authored_bundle.effective_ports] == [("api", 5678)]


def test_a_public_slot_consumption_without_a_public_face_refuses(tmp_path: Path) -> None:
    bundle = BUNDLE.replace(
        "      TOKEN: ${API_TOKEN:?err}\n", "      TOKEN: ${API_TOKEN:?err}\n      HOST: ${PUBLIC_HOST:?err}\n"
    )

    message = _refusal(tmp_path, _filled_values(), bundle=bundle)

    assert "core-main consumes PUBLIC_HOST, which requires public_host in the values file" in message


def test_an_orphan_key_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    hand = values["values"]
    assert isinstance(hand, dict)
    hand["GHOST"] = "x"

    message = _refusal(tmp_path, values)

    assert "GHOST is consumed by no surface" in message


def test_the_minted_public_slots_refuse(tmp_path: Path) -> None:
    for index, name in enumerate(("PUBLIC_HOST", "PUBLIC_SCHEME", "PUBLIC_PORT", "PUBLIC_URL_PATH", "PUBLIC_URL")):
        values = _filled_values({"values": {name: "x"}})
        case = tmp_path / f"case-{index}"
        case.mkdir()

        message = _refusal(case, values)

        assert f"{name} is not hand-settable (minted from the public face)" in message


def test_the_minted_edge_base_url_key_refuses(tmp_path: Path) -> None:
    values = _filled_values({"values": {"EDGE_BASE_URL": "http://boatyard-gateway:8443"}})

    message = _refusal(tmp_path, values, gateway=9443)

    assert "EDGE_BASE_URL is not hand-settable (minted from the public face)" in message


def test_the_minted_edge_base_url_key_refuses_without_a_gateway(tmp_path: Path) -> None:
    values = _filled_values({"values": {"EDGE_BASE_URL": "http://boatyard-gateway:8443"}})

    message = _refusal(tmp_path, values)

    assert "EDGE_BASE_URL is not hand-settable (minted from the public face)" in message


def test_an_unknown_ports_override_refuses(tmp_path: Path) -> None:
    values = _filled_values({"ports": {"ghost": 1234}})

    message = _refusal(tmp_path, values, ports=[{"service": "api", "published": 5678, "target": 5678}])

    assert "ports: names ['ghost'] are services of no selected bundle" in message


def test_a_ports_override_on_a_multi_port_service_refuses(tmp_path: Path) -> None:
    values = _filled_values({"ports": {"api": 1234}})

    message = _refusal(
        tmp_path,
        values,
        ports=[
            {"service": "api", "published": 5678, "target": 5678},
            {"service": "api", "published": 5679, "target": 5679, "protocol": "udp"},
        ],
    )

    assert "ports: ['api'] carry a count of effective ports a scalar cannot disambiguate" in message


def test_a_gateway_override_without_a_declared_gateway_refuses(tmp_path: Path) -> None:
    values = _filled_values({"gateway_port": 9000})

    message = _refusal(tmp_path, values)

    assert "gateway_port is set but boatyard.yml declares no gateway" in message


def test_typed_port_and_gateway_overrides_load(tmp_path: Path) -> None:
    values = _filled_values({"gateway_port": 9000, "ports": {"core-main": 1234}})

    parsed = _load(
        _write_repo(
            tmp_path,
            values,
            gateway=58080,
            ports=[{"service": "api", "published": 5678, "target": 5678}],
        )
    )

    assert parsed.gateway_port == 9000
    assert parsed.ports == {"core-main": 1234}


def test_an_optional_slot_stays_unfilled(tmp_path: Path) -> None:
    parsed = _load(
        _write_repo(
            tmp_path,
            _filled_values(),
            slots={"MON_TOKEN": {"required": True}, "MON_OPT": {"required": False}},
        )
    )

    assert parsed.values == {
        "API_TOKEN": "one",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }
    assert "MON_OPT" not in parsed.values


def test_consumer_interpolated_content_demands(tmp_path: Path) -> None:
    values = _filled_values()
    hand = values["values"]
    assert isinstance(hand, dict)
    del hand["BIND_TOKEN"]

    message = _refusal(tmp_path, values)

    assert "core-main requires ['BIND_TOKEN']" in message


def test_provisioner_services_text_demands(tmp_path: Path) -> None:
    values = _filled_values()
    hand = values["values"]
    assert isinstance(hand, dict)
    del hand["PROV_TOKEN"]

    message = _refusal(tmp_path, values)

    assert "core-main requires ['PROV_TOKEN']" in message


def test_a_marker_lock_key_spelling_leaves_the_surface(tmp_path: Path) -> None:
    bundle = BUNDLE.replace(
        f"image: ghcr.io/org/loki:3.0@{FAKE_DIGEST}", "image: ${CORE_MAIN__BOATYARD_LOCK__LOKI_IMAGE:?err}"
    )
    values = _filled_values()
    root = _write_repo(tmp_path, values, bundle=bundle)

    assert "LOKI_IMAGE" not in _load(root).values
    assert [bundle.entry_name for bundle in _resolve(root).bundles] == ["core-main", "monitor"]


def test_a_key_parked_on_an_unselected_option_resolves(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, _filled_values({"bundles": []}))
    (root / "stack" / "score" / "pf.yaml").write_text("metadata:\n  name: pf\ncontainers: {}\n", encoding="utf-8")
    config = yaml.safe_load((root / "boatyard.yml").read_text(encoding="utf-8"))
    config["services"]["placeframe"] = {"options": {"cuda": {"workloads": ["pf.yaml"]}}}
    (root / "boatyard.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    option_output = root / "stack" / "generated" / "compose" / "compose.placeframe-cuda.yaml"
    option_output.write_text(
        "services:\n  placeframe-cuda-pf:\n    image: demo:1\n    environment:\n      PF_SETTING: ${PF_SETTING:?err}\n",
        encoding="utf-8",
    )
    values = yaml.safe_load((root / VALUES_FILE).read_text(encoding="utf-8"))
    values["values"]["PF_SETTING"] = "x"
    (root / VALUES_FILE).write_text(yaml.safe_dump(values), encoding="utf-8")

    assert [bundle.entry_name for bundle in _resolve(root).bundles] == ["monitor"]
    assert _resolve(root).env["PF_SETTING"] == "x"


def test_an_absent_override_file_refuses_at_load(tmp_path: Path) -> None:
    values = _filled_values({"path_overrides": {"monitor": "../producer/stack/generated/compose/compose.monitor.yaml"}})

    message = _refusal(tmp_path, values)

    assert "path_overrides.monitor" in message
    assert "does not exist" in message


def test_an_override_demands_from_its_artifact_text(tmp_path: Path) -> None:
    values = _filled_values({"path_overrides": {"monitor": "../producer/stack/generated/compose/compose.monitor.yaml"}})
    override = tmp_path / "producer" / "stack" / "generated" / "compose" / "compose.monitor.yaml"
    override.parent.mkdir(parents=True)
    (tmp_path / "producer" / "boatyard.yml").write_text(
        yaml.safe_dump({
            "requires": ">=0",
            "project_name": "producer",
            "services": {"stub": {"ref": "oci://ghcr.io/org/stub:0.0.1"}},
        }),
        encoding="utf-8",
    )
    override.write_text(
        "services:\n"
        "  monitor-monitor:\n"
        "    image: demo/monitor:1\n"
        "    environment:\n"
        "      TOKEN: ${MON_TOKEN:?err}\n"
        "      OVR_TOKEN: ${OVR_TOKEN:?err}\n",
        encoding="utf-8",
    )

    message = _refusal(tmp_path, values)

    assert "monitor requires ['OVR_TOKEN']" in message


def test_a_reference_to_a_sibling_key_resolves_at_render(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${PROV_TOKEN}-token")
    root = _write_repo(tmp_path, values)

    assert _load(root).values == {
        "API_TOKEN": "${PROV_TOKEN}-token",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }
    assert _resolve(root).env == {
        "API_TOKEN": "three-token",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
        "CORE_MAIN__BOATYARD_LOCK__ALPINE_IMAGE": f"ghcr.io/org/alpine:3.20@{FAKE_DIGEST}",
        "CORE_MAIN__BOATYARD_LOCK__LOKI_IMAGE": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}",
    }


def test_a_reference_chains_through_another_reference(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="u${BIND_TOKEN}", BIND_TOKEN="${PROV_TOKEN}b")
    root = _write_repo(tmp_path, values)

    assert _resolve(root).env == {
        "API_TOKEN": "uthreeb",
        "BIND_TOKEN": "threeb",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
        "CORE_MAIN__BOATYARD_LOCK__ALPINE_IMAGE": f"ghcr.io/org/alpine:3.20@{FAKE_DIGEST}",
        "CORE_MAIN__BOATYARD_LOCK__LOKI_IMAGE": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}",
    }


def test_the_minted_public_url_is_a_reference_root(tmp_path: Path) -> None:
    values = _filled_values({"public_host": "example.dev", "public_scheme": "https"})
    values["values"] = _core_values(API_TOKEN="${PUBLIC_URL}/token")
    root = _write_repo(tmp_path, values)

    assert _resolve(root).env == {
        "PUBLIC_HOST": "example.dev",
        "PUBLIC_SCHEME": "https",
        "PUBLIC_PORT": "443",
        "PUBLIC_URL_PATH": "",
        "PUBLIC_URL": "https://example.dev",
        "API_TOKEN": "https://example.dev/token",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
        "CORE_MAIN__BOATYARD_LOCK__ALPINE_IMAGE": f"ghcr.io/org/alpine:3.20@{FAKE_DIGEST}",
        "CORE_MAIN__BOATYARD_LOCK__LOKI_IMAGE": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}",
    }


def test_the_minted_public_url_elides_the_scheme_default_port(tmp_path: Path) -> None:
    values = _filled_values({"public_host": "example.dev"})
    values["values"] = _core_values(API_TOKEN="${PUBLIC_URL}/token")
    root = _write_repo(tmp_path, values)

    env = _resolve(root).env

    assert env["PUBLIC_PORT"] == "80"
    assert env["PUBLIC_URL"] == "http://example.dev"


def test_the_minted_public_url_derives_the_port_from_the_gateway(tmp_path: Path) -> None:
    values = _filled_values({"public_host": "example.dev", "public_url_path": "/makeitsing"})
    values["values"] = _core_values(API_TOKEN="${PUBLIC_URL}/token")
    root = _write_repo(tmp_path, values, gateway=58080)

    env = _resolve(root).env

    assert env["PUBLIC_PORT"] == "58080"
    assert env["PUBLIC_URL"] == "http://example.dev:58080/makeitsing"


def test_the_minted_public_url_is_no_root_when_public_host_is_absent(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${PUBLIC_URL}/token")

    message = _refusal(tmp_path, values)

    assert "${PUBLIC_URL} resolves to no root" in message


def test_the_minted_edge_base_url_is_a_reference_root(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${EDGE_BASE_URL}/jwks")
    root = _write_repo(tmp_path, values, gateway=9443)

    assert _resolve(root).env == {
        "EDGE_BASE_URL": "http://boatyard-gateway:8443",
        "API_TOKEN": "http://boatyard-gateway:8443/jwks",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
        "CORE_MAIN__BOATYARD_LOCK__ALPINE_IMAGE": f"ghcr.io/org/alpine:3.20@{FAKE_DIGEST}",
        "CORE_MAIN__BOATYARD_LOCK__CADDY_IMAGE": f"ghcr.io/org/caddy:2.8@{FAKE_DIGEST}",
        "CORE_MAIN__BOATYARD_LOCK__LOKI_IMAGE": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}",
    }


def test_the_minted_edge_base_url_is_no_root_when_no_gateway_is_declared(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${EDGE_BASE_URL}/jwks")

    message = _refusal(tmp_path, values)

    assert "${EDGE_BASE_URL} resolves to no root" in message


def test_an_unresolvable_reference_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${GHOST}")

    message = _refusal(tmp_path, values)

    assert "${GHOST} resolves to no root" in message


def test_a_default_spelling_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${PROV_TOKEN:-three}")

    message = _refusal(tmp_path, values)

    assert "only bare ${NAME} references are legal" in message


def test_a_lowercase_reference_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${public_host}/token")

    message = _refusal(tmp_path, values)

    assert "only bare ${NAME} references are legal" in message


def test_a_nested_reference_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${${PROV_TOKEN}}")

    message = _refusal(tmp_path, values)

    assert "only bare ${NAME} references are legal" in message


def test_a_marker_reference_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${CORE_MAIN__BOATYARD_LOCK__PROV_TOKEN}")

    message = _refusal(tmp_path, values)

    assert "${CORE_MAIN__BOATYARD_LOCK__PROV_TOKEN} must not carry a reserved marker" in message


def test_a_reference_cycle_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="x${BIND_TOKEN}", BIND_TOKEN="y${API_TOKEN}")

    message = _refusal(tmp_path, values)

    assert "reference cycle: API_TOKEN -> BIND_TOKEN -> API_TOKEN" in message


def test_a_self_reference_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="x${API_TOKEN}")

    message = _refusal(tmp_path, values)

    assert "reference cycle: API_TOKEN -> API_TOKEN" in message


def test_assembly_renders_resolved_values_with_the_minted_slot(tmp_path: Path) -> None:
    values = _filled_values({"public_host": "example.dev", "public_scheme": "https"})
    values["values"] = _core_values(API_TOKEN="${PUBLIC_URL}/token")
    root = _write_repo(tmp_path, values)

    plan = _resolve(root)

    assert plan.env == {
        "PUBLIC_HOST": "example.dev",
        "PUBLIC_SCHEME": "https",
        "PUBLIC_PORT": "443",
        "PUBLIC_URL_PATH": "",
        "PUBLIC_URL": "https://example.dev",
        "API_TOKEN": "https://example.dev/token",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
        "CORE_MAIN__BOATYARD_LOCK__ALPINE_IMAGE": f"ghcr.io/org/alpine:3.20@{FAKE_DIGEST}",
        "CORE_MAIN__BOATYARD_LOCK__LOKI_IMAGE": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}",
    }


def test_assembly_fills_the_minted_edge_base_url(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"] = _core_values(API_TOKEN="${EDGE_BASE_URL}/jwks")
    root = _write_repo(tmp_path, values, gateway=9443)

    plan = _resolve(root)

    assert plan.env == {
        "EDGE_BASE_URL": "http://boatyard-gateway:8443",
        "API_TOKEN": "http://boatyard-gateway:8443/jwks",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
        "CORE_MAIN__BOATYARD_LOCK__ALPINE_IMAGE": f"ghcr.io/org/alpine:3.20@{FAKE_DIGEST}",
        "CORE_MAIN__BOATYARD_LOCK__CADDY_IMAGE": f"ghcr.io/org/caddy:2.8@{FAKE_DIGEST}",
        "CORE_MAIN__BOATYARD_LOCK__LOKI_IMAGE": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}",
    }
