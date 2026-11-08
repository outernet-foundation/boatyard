from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from boatyard.config.config import ConfigFile
from boatyard.config.lock import LOCK_PATH, LockFile
from boatyard.config.values import ValuesFile
from boatyard.paths import VALUES_FILE

FAKE_DIGEST = "sha256:" + "a" * 64
MONITOR_PINNED = f"ghcr.io/org/monitor:1.0.0@{FAKE_DIGEST}"

WORKLOAD = """\
metadata:
  name: api
  annotations:
    outernet.internal/consumer-interpolated: /etc/rathole/client.toml
containers:
  api:
    image: ${LOKI_IMAGE}
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
    workload: str = WORKLOAD,
    secrets: object = {},
    gateway: int | None = None,
    ports: list[dict[str, object]] | None = None,
) -> Path:
    root = tmp_path / "consumer"
    root.mkdir()
    score = root / "stack" / "score"
    score.mkdir(parents=True)
    (score / "api.yaml").write_text(workload, encoding="utf-8")
    (score / "compose").mkdir()
    (score / "compose" / "services.yaml").write_text(PROVISIONER, encoding="utf-8")
    base_images: dict[str, str] = {"LOKI_IMAGE": "docker.io/grafana/loki:3.0"}
    core_entry: dict[str, object] = {"workloads": ["api.yaml"]}
    if ports is not None:
        core_entry["ports"] = ports
    config: dict[str, object] = {
        "requires": ">=0",
        "project_name": "demo",
        "repository": "ghcr.io/org/demo",
        "platforms": ["linux/amd64"],
        "base_images": base_images,
        "secrets": secrets,
        "services": {
            "core": core_entry,
            "monitor": {"ref": "oci://ghcr.io/org/monitor:1.0.0"},
        },
    }
    if gateway is not None:
        config["gateway"] = gateway
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
            "bundles": {"MONITOR_BUNDLE": {"reference": MONITOR_PINNED, "slots": _monitor_slots(slots)}},
        }),
        encoding="utf-8",
    )
    (root / VALUES_FILE).write_text(yaml.safe_dump(values), encoding="utf-8")
    return root


def _filled_values(extra: dict[str, object] | None = None) -> dict[str, object]:
    values: dict[str, object] = {
        "values": {
            "images": {"core": {"API_TOKEN": "one", "BIND_TOKEN": "two", "PROV_TOKEN": "three"}},
            "bundles": {"monitor": {"MON_TOKEN": "four"}},
        }
    }
    if extra is not None:
        values.update(extra)
    return values


def _core_values(**overrides: str) -> dict[str, str]:
    section = {"API_TOKEN": "one", "BIND_TOKEN": "two", "PROV_TOKEN": "three"}
    section.update(overrides)
    return section


def _load(root: Path) -> ValuesFile:
    config = ConfigFile.load(root / "boatyard.yml")
    lock = LockFile.load(root / LOCK_PATH, config)
    return ValuesFile.load(root / VALUES_FILE, config, lock)


def _refusal(
    tmp_path: Path,
    values: dict[str, object],
    *,
    slots: dict[str, object] | None = None,
    workload: str = WORKLOAD,
    secrets: object = {},
    gateway: int | None = None,
    ports: list[dict[str, object]] | None = None,
) -> str:
    with pytest.raises(ValidationError) as report:
        _load(
            _write_repo(tmp_path, values, slots=slots, workload=workload, secrets=secrets, gateway=gateway, ports=ports)
        )
    return str(report.value)


def test_a_filled_surface_loads_and_flattens(tmp_path: Path) -> None:
    parsed = _load(_write_repo(tmp_path, _filled_values(), slots={"MON_TOKEN": {"required": True}}))

    assert parsed.choices == {}
    assert parsed.flattened == {
        "API_TOKEN": "one",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }


def test_an_orphan_key_refuses_naming_the_section(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"]["GHOST"] = "x"  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "values.images.core: GHOST is demanded by no surface of core" in message


def test_a_key_demanded_by_two_units_refuses_naming_shared(tmp_path: Path) -> None:
    values = _filled_values()
    del values["values"]["bundles"]["monitor"]["MON_TOKEN"]  # type: ignore[index]

    message = _refusal(tmp_path, values, slots={"API_TOKEN": {"required": False}, "MON_TOKEN": {"required": True}})

    assert "values.images.core: API_TOKEN is demanded by core, monitor; set it in values.shared" in message


def test_a_shared_key_demanded_by_one_unit_refuses_naming_its_home(tmp_path: Path) -> None:
    values = _filled_values()
    del values["values"]["images"]["core"]["PROV_TOKEN"]  # type: ignore[index]
    values["values"]["shared"] = {"PROV_TOKEN": "three"}  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "values.shared: PROV_TOKEN is demanded by core only; set it in values.images.core" in message


def test_a_shared_key_demanded_by_nothing_refuses(tmp_path: Path) -> None:
    values = _filled_values({"values": {"shared": {"ORPHAN": "x"}}})
    values["values"]["images"] = {}  # type: ignore[index]
    values["values"]["bundles"] = {}  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "values.shared: ORPHAN is demanded by no surface" in message


def test_the_minted_public_url_key_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["shared"] = {"PUBLIC_URL": "https://example.dev"}  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "PUBLIC_URL is minted from public_url" in message


def test_the_minted_edge_base_url_key_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["shared"] = {"EDGE_BASE_URL": "http://edge:8443"}  # type: ignore[index]

    message = _refusal(tmp_path, values, gateway=9443)

    assert "EDGE_BASE_URL is minted from the gateway declaration" in message


def test_the_minted_edge_base_url_key_refuses_without_a_gateway(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["shared"] = {"EDGE_BASE_URL": "http://edge:8443"}  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "EDGE_BASE_URL is minted from the gateway declaration" in message


def test_an_unknown_ports_override_refuses_listing_the_vocabulary(tmp_path: Path) -> None:
    values = _filled_values({"ports": {"ghost": 1234}})

    message = _refusal(tmp_path, values, ports=[{"service": "api", "published": 5678, "target": 5678}])

    assert "ports: names ['ghost']; the port-declaring entries are ['core']" in message


def test_a_gateway_override_without_a_declared_gateway_refuses(tmp_path: Path) -> None:
    values = _filled_values({"gateway": 9000})

    message = _refusal(tmp_path, values)

    assert "gateway: is set but boatyard.yml declares no gateway" in message


def test_typed_port_and_gateway_overrides_load(tmp_path: Path) -> None:
    values = _filled_values({"gateway": 9000, "ports": {"core": 1234}})

    parsed = _load(
        _write_repo(
            tmp_path,
            values,
            gateway=58080,
            ports=[{"service": "api", "published": 5678, "target": 5678}],
        )
    )

    assert parsed.gateway == 9000
    assert parsed.ports == {"core": 1234}


def test_an_optional_demand_rides_unfilled_when_declared(tmp_path: Path) -> None:
    parsed = _load(
        _write_repo(
            tmp_path,
            _filled_values(),
            slots={"MON_TOKEN": {"required": True}, "MON_OPT": {"required": False}},
            secrets={"MON_OPT": "declared but unfilled"},
        )
    )

    assert parsed.flattened == {
        "API_TOKEN": "one",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }
    assert "MON_OPT" not in parsed.flattened


def test_an_undeclared_consumed_key_refuses_at_load(tmp_path: Path) -> None:
    values = _filled_values()

    message = _refusal(tmp_path, values, slots={"MON_TOKEN": {}, "MON_OPT": {"required": False}})

    assert "consumed keys ['MON_OPT'] are declared nowhere" in message


def test_a_secrets_declaration_covers_a_demand(tmp_path: Path) -> None:
    values = _filled_values()

    parsed = _load(_write_repo(tmp_path, values, slots={"MON_TOKEN": {}}, secrets={"PROV_TOKEN": "declared"}))

    assert "PROV_TOKEN" in parsed.flattened


def test_consumer_interpolated_content_demands(tmp_path: Path) -> None:
    values = _filled_values()
    del values["values"]["images"]["core"]["BIND_TOKEN"]  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "values.images.core requires ['BIND_TOKEN']" in message


def test_provisioner_services_text_demands(tmp_path: Path) -> None:
    values = _filled_values()
    del values["values"]["images"]["core"]["PROV_TOKEN"]  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "values.images.core requires ['PROV_TOKEN']" in message


def test_a_suffixed_lock_key_spelling_leaves_the_surface(tmp_path: Path) -> None:
    workload = WORKLOAD.replace("image: ${LOKI_IMAGE}", "image: ${LOKI_IMAGE:?err}")
    values = _filled_values()

    parsed = _load(_write_repo(tmp_path, values, workload=workload))

    assert "LOKI_IMAGE" not in parsed.flattened


def test_a_bare_workload_slot_is_not_a_demand(tmp_path: Path) -> None:
    workload = WORKLOAD.replace("TOKEN: ${API_TOKEN:?err}", "VERSION: ${API_SHA}")
    values = _filled_values()
    del values["values"]["images"]["core"]["API_TOKEN"]  # type: ignore[index]

    parsed = _load(_write_repo(tmp_path, values, workload=workload))

    assert "API_SHA" not in parsed.flattened


def test_an_unselected_options_section_still_orphan_refuses(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, _filled_values({"bundles": []}))
    (root / "stack" / "score" / "pf.yaml").write_text("metadata:\n  name: pf\ncontainers: {}\n", encoding="utf-8")
    config = yaml.safe_load((root / "boatyard.yml").read_text(encoding="utf-8"))
    config["services"]["placeframe"] = {"options": {"cuda": {"workloads": ["pf.yaml"]}}}
    (root / "boatyard.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    values = yaml.safe_load((root / VALUES_FILE).read_text(encoding="utf-8"))
    values["values"]["images"]["placeframe-cuda"] = {"GHOST": "x"}
    (root / VALUES_FILE).write_text(yaml.safe_dump(values), encoding="utf-8")

    with pytest.raises(ValidationError, match="demanded by no surface"):
        _load(root)


def test_an_absent_override_file_refuses_at_load(tmp_path: Path) -> None:
    values = _filled_values({"path_overrides": {"monitor": "../producer/stack/generated/compose/compose.monitor.yaml"}})

    message = _refusal(tmp_path, values)

    assert "path_overrides.monitor" in message
    assert "does not exist" in message


def test_an_override_demands_from_its_artifact_text(tmp_path: Path) -> None:
    values = _filled_values({"path_overrides": {"monitor": "../producer/stack/generated/compose/compose.monitor.yaml"}})
    override = tmp_path / "producer" / "stack" / "generated" / "compose" / "compose.monitor.yaml"
    override.parent.mkdir(parents=True)
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

    assert "values.bundles.monitor requires ['OVR_TOKEN']" in message


def test_a_fill_refusal_names_shell_set_values_as_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = _filled_values()
    del values["values"]["images"]["core"]["API_TOKEN"]  # type: ignore[index]
    monkeypatch.setenv("API_TOKEN", "shell-value")

    message = _refusal(tmp_path, values)

    assert "API_TOKEN set in the shell and ignored" in message


def test_a_reference_to_a_sibling_key_resolves_at_render(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${PROV_TOKEN}-token")  # type: ignore[index]

    parsed = _load(_write_repo(tmp_path, values))

    assert parsed.flattened == {
        "API_TOKEN": "${PROV_TOKEN}-token",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }
    assert parsed.resolved == {
        "API_TOKEN": "three-token",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }


def test_a_reference_chains_through_another_reference(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(  # type: ignore[index]
        API_TOKEN="u${BIND_TOKEN}", BIND_TOKEN="${PROV_TOKEN}b"
    )

    parsed = _load(_write_repo(tmp_path, values))

    assert parsed.resolved == {
        "API_TOKEN": "uthreeb",
        "BIND_TOKEN": "threeb",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }


def test_the_minted_public_url_is_a_reference_root(tmp_path: Path) -> None:
    values = _filled_values({"public_url": "https://example.dev"})
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${PUBLIC_URL}/token")  # type: ignore[index]

    parsed = _load(_write_repo(tmp_path, values))

    assert parsed.resolved == {
        "PUBLIC_URL": "https://example.dev",
        "API_TOKEN": "https://example.dev/token",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }


def test_the_minted_public_url_is_no_root_when_public_url_is_absent(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${PUBLIC_URL}/token")  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "${PUBLIC_URL} resolves to no root" in message


def test_the_minted_edge_base_url_is_a_reference_root(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${EDGE_BASE_URL}/jwks")  # type: ignore[index]

    parsed = _load(_write_repo(tmp_path, values, gateway=9443))

    assert parsed.resolved == {
        "EDGE_BASE_URL": "http://edge:8443",
        "API_TOKEN": "http://edge:8443/jwks",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }


def test_the_minted_edge_base_url_is_no_root_when_no_gateway_is_declared(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${EDGE_BASE_URL}/jwks")  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "${EDGE_BASE_URL} resolves to no root" in message


def test_an_unresolvable_reference_refuses_listing_the_roots(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${GHOST}")  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "${GHOST} resolves to no root" in message
    assert "the roots are ['API_TOKEN', 'BIND_TOKEN', 'MON_TOKEN', 'PROV_TOKEN']" in message


def test_a_default_spelling_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${PROV_TOKEN:-three}")  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "only bare ${NAME} references are legal" in message


def test_a_lowercase_reference_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${public_url}/token")  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "only bare ${NAME} references are legal" in message


def test_a_nested_reference_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${${PROV_TOKEN}}")  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "only bare ${NAME} references are legal" in message


def test_a_marker_reference_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(  # type: ignore[index]
        API_TOKEN="${CORE__BOATYARD_LOCK__PROV_TOKEN}"
    )

    message = _refusal(tmp_path, values)

    assert "carries a reserved __BOATYARD_LOCK__/__BOATYARD_TREE__ marker" in message


def test_a_reference_cycle_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(  # type: ignore[index]
        API_TOKEN="x${BIND_TOKEN}", BIND_TOKEN="y${API_TOKEN}"
    )

    message = _refusal(tmp_path, values)

    assert "reference cycle: API_TOKEN -> BIND_TOKEN -> API_TOKEN" in message


def test_a_self_reference_refuses(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="x${API_TOKEN}")  # type: ignore[index]

    message = _refusal(tmp_path, values)

    assert "reference cycle: API_TOKEN -> API_TOKEN" in message


def test_assembly_renders_resolved_values_with_the_minted_slot(tmp_path: Path) -> None:
    values = _filled_values({"public_url": "https://example.dev"})
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${PUBLIC_URL}/token")  # type: ignore[index]
    root = _write_repo(tmp_path, values)
    bundle_output = root / "stack" / "generated" / "compose" / "compose.core.yaml"
    bundle_output.parent.mkdir(parents=True)
    bundle_output.write_text("services:\n  core-api:\n    image: demo:1\n", encoding="utf-8")

    plan = _load(root).assembly()

    assert plan.values == {
        "PUBLIC_URL": "https://example.dev",
        "API_TOKEN": "https://example.dev/token",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }


def test_assembly_fills_the_minted_edge_base_url(tmp_path: Path) -> None:
    values = _filled_values()
    values["values"]["images"]["core"] = _core_values(API_TOKEN="${EDGE_BASE_URL}/jwks")  # type: ignore[index]
    root = _write_repo(tmp_path, values, gateway=9443)
    bundle_output = root / "stack" / "generated" / "compose" / "compose.core.yaml"
    bundle_output.parent.mkdir(parents=True)
    bundle_output.write_text("services:\n  core-api:\n    image: demo:1\n", encoding="utf-8")

    plan = _load(root).assembly()

    assert plan.values == {
        "EDGE_BASE_URL": "http://edge:8443",
        "API_TOKEN": "http://edge:8443/jwks",
        "BIND_TOKEN": "two",
        "PROV_TOKEN": "three",
        "MON_TOKEN": "four",
    }
