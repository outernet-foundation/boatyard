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
) -> Path:
    root = tmp_path / "consumer"
    root.mkdir()
    score = root / "stack" / "score"
    score.mkdir(parents=True)
    (score / "api.yaml").write_text(workload, encoding="utf-8")
    (score / "compose").mkdir()
    (score / "compose" / "services.yaml").write_text(PROVISIONER, encoding="utf-8")
    (root / "boatyard.yml").write_text(
        yaml.safe_dump({
            "requires": ">=0",
            "project_name": "demo",
            "repository": "ghcr.io/org/demo",
            "platforms": ["linux/amd64"],
            "base_images": {"LOKI_IMAGE": "docker.io/grafana/loki:3.0"},
            "secrets": secrets,
            "services": {
                "core": {"workloads": ["api.yaml"]},
                "monitor": {"ref": "oci://ghcr.io/org/monitor:1.0.0"},
            },
        }),
        encoding="utf-8",
    )
    root.joinpath(*LOCK_PATH.parts).write_text(
        yaml.safe_dump({
            "platforms": ["linux/amd64"],
            "images": {
                "LOKI_IMAGE": {
                    "index": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}",
                    "platforms": {"linux/amd64": f"ghcr.io/org/loki:3.0@{FAKE_DIGEST}"},
                }
            },
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
) -> str:
    with pytest.raises(ValidationError) as report:
        _load(_write_repo(tmp_path, values, slots=slots, workload=workload, secrets=secrets))
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
