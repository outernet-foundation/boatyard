from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from boatyard.models.image import Image
from boatyard.yaml import is_mapping
from boatyard.markers import MarkerContext, declared_dockerfile_paths, resolve_markers

RIVER = """// relay config
loki.write "default" {
  url = sys.env("LOKI_WRITE_ENDPOINT")
}
"""

DATASOURCES = """apiVersion: 1
datasources:
  - name: Loki
    url: http://loki:3100
"""

DIGEST = "sha256:" + "a" * 64
ALLOY = "ghcr.io/outernet-foundation/mirror/docker.io/grafana/alloy:v1.9.0@" + DIGEST
LOKI = "ghcr.io/outernet-foundation/mirror/docker.io/grafana/loki:3.5.0@" + DIGEST


def write_repo(tree: dict[str, str], root: Path) -> None:
    for name, content in tree.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_text_include_inserts_file_bytes_verbatim(tmp_path: Path) -> None:
    write_repo({"workloads/alloy/config.alloy": RIVER}, tmp_path)

    resolved = resolve_markers(
        {"config.alloy": {"include": "workloads/alloy/config.alloy"}}, MarkerContext(tmp_path, {}, "x.yaml")
    )

    assert resolved == {"config.alloy": RIVER}


def test_text_include_leaves_dollar_slots_untouched(tmp_path: Path) -> None:
    write_repo({"config.yaml": "key: ${S3_ACCESS_KEY}\n"}, tmp_path)

    resolved = resolve_markers({"include": "config.yaml"}, MarkerContext(tmp_path, {}, "x.yaml"))

    assert resolved == "key: ${S3_ACCESS_KEY}\n"


def test_yaml_include_inserts_the_parsed_structure(tmp_path: Path) -> None:
    write_repo({"workloads/grafana/provisioning/datasources/datasources.yaml": DATASOURCES}, tmp_path)

    resolved = resolve_markers(
        {
            "datasources": {
                "datasources.yaml": {"include-yaml": "workloads/grafana/provisioning/datasources/datasources.yaml"}
            }
        },
        MarkerContext(tmp_path, {}, "values.yaml"),
    )

    assert resolved == {"datasources": {"datasources.yaml": yaml.safe_load(DATASOURCES)}}


def test_markers_resolve_inside_lists_and_nested_mappings(tmp_path: Path) -> None:
    write_repo({"a.txt": "alpha\n", "b.txt": "beta\n"}, tmp_path)

    resolved = resolve_markers(
        {"items": [{"include": "a.txt"}, {"deep": {"include": "b.txt"}}]}, MarkerContext(tmp_path, {}, "x.yaml")
    )

    assert resolved == {"items": ["alpha\n", {"deep": "beta\n"}]}


def test_marker_with_extra_keys_is_plain_data(tmp_path: Path) -> None:
    resolved = resolve_markers({"include": "a.txt", "other": 1}, MarkerContext(tmp_path, {}, "x.yaml"))

    assert resolved == {"include": "a.txt", "other": 1}


def test_missing_include_is_a_loud_error_naming_the_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"values\.yaml.*'workloads/alloy/config.alloy' does not exist"):
        resolve_markers({"include": "workloads/alloy/config.alloy"}, MarkerContext(tmp_path, {}, "values.yaml"))


def test_absolute_include_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="anchor at the repo root"):
        resolve_markers({"include": "/etc/passwd"}, MarkerContext(tmp_path, {}, "x.yaml"))


def test_parent_escaping_include_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="anchor at the repo root"):
        resolve_markers({"include": "../outside.txt"}, MarkerContext(tmp_path, {}, "x.yaml"))


def test_non_string_marker_value_is_a_loud_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path string"):
        resolve_markers({"include": ["a", "b"]}, MarkerContext(tmp_path, {}, "x.yaml"))


def test_invalid_yaml_include_is_a_loud_error(tmp_path: Path) -> None:
    write_repo({"broken.yaml": "key: [unclosed\n"}, tmp_path)

    with pytest.raises(ValueError, match="not valid YAML"):
        resolve_markers({"include-yaml": "broken.yaml"}, MarkerContext(tmp_path, {}, "x.yaml"))


def test_empty_yaml_include_is_a_loud_error(tmp_path: Path) -> None:
    write_repo({"empty.yaml": ""}, tmp_path)

    with pytest.raises(ValueError, match="is empty"):
        resolve_markers({"include-yaml": "empty.yaml"}, MarkerContext(tmp_path, {}, "x.yaml"))


def test_yaml_include_cycles_are_detected(tmp_path: Path) -> None:
    write_repo({"a.yaml": "a: {include-yaml: b.yaml}\n", "b.yaml": "b: {include-yaml: a.yaml}\n"}, tmp_path)

    with pytest.raises(ValueError, match="cycle"):
        resolve_markers({"include-yaml": "a.yaml"}, MarkerContext(tmp_path, {}, "x.yaml"))


def test_yaml_includes_resolve_recursively(tmp_path: Path) -> None:
    write_repo({"outer.yaml": "inner: {include-yaml: inner.yaml}\n", "inner.yaml": "value: 1\n"}, tmp_path)

    resolved = resolve_markers({"include-yaml": "outer.yaml"}, MarkerContext(tmp_path, {}, "x.yaml"))

    assert resolved == {"inner": {"value": 1}}


def test_text_include_content_is_never_scanned_for_markers(tmp_path: Path) -> None:
    write_repo({"a.txt": "include: b.txt\n"}, tmp_path)

    resolved = resolve_markers({"include": "a.txt"}, MarkerContext(tmp_path, {}, "x.yaml"))

    assert resolved == "include: b.txt\n"


def test_non_marker_trees_pass_through_unchanged(tmp_path: Path) -> None:
    tree: Any = {"plain": "value", "list": [1, 2, {"nested": True}]}

    assert resolve_markers(tree, MarkerContext(tmp_path, {}, "x.yaml")) == tree


def test_lock_marker_substitutes_the_full_reference() -> None:
    resolved = resolve_markers(
        {"image": {"lock": "ALLOY_IMAGE"}}, MarkerContext(Path(), {"ALLOY_IMAGE": ALLOY}, "k8s.yaml")
    )

    assert resolved == {"image": ALLOY}


def test_lock_marker_resolves_inside_lists_and_nested_mappings() -> None:
    source: Any = {"manifests": [{"containers": [{"image": {"lock": "ALLOY_IMAGE"}}]}]}

    resolved = resolve_markers(source, MarkerContext(Path(), {"ALLOY_IMAGE": ALLOY}, "k8s.yaml"))

    assert resolved == {"manifests": [{"containers": [{"image": ALLOY}]}]}


def test_unknown_lock_entry_is_a_loud_error_naming_the_source() -> None:
    with pytest.raises(ValueError, match=r"k8s\.yaml.*'KEYCLOAK_IMAGE' does not exist"):
        resolve_markers(
            {"image": {"lock": "KEYCLOAK_IMAGE"}}, MarkerContext(Path(), {"ALLOY_IMAGE": ALLOY}, "k8s.yaml")
        )


def test_non_string_lock_marker_value_is_a_loud_error() -> None:
    with pytest.raises(ValueError, match="name string"):
        resolve_markers({"image": {"lock": ["ALLOY"]}}, MarkerContext(Path(), {"ALLOY_IMAGE": ALLOY}, "k8s.yaml"))


def test_marker_with_extra_keys_is_plain_data_under_lock_resolution() -> None:
    node: Any = {"lock": "ALLOY_IMAGE", "tag": "v1"}

    resolved = resolve_markers(node, MarkerContext(Path(), {"ALLOY_IMAGE": ALLOY, "tag": "v1"}, "k8s.yaml"))

    assert resolved == node


def test_lock_image_marker_splits_chart_fields() -> None:
    resolved = resolve_markers(
        {"loki": {"image": {"lock-image": "LOKI_IMAGE"}}}, MarkerContext(Path(), {"LOKI_IMAGE": LOKI}, "loki.yaml")
    )

    assert resolved == {
        "loki": {
            "image": {
                "registry": "ghcr.io/outernet-foundation/mirror/docker.io/grafana",
                "repository": "loki",
                "tag": "3.5.0",
                "digest": DIGEST,
            }
        }
    }


def test_lock_image_marker_splits_field_order_matches_chart_convention() -> None:
    resolved = resolve_markers(
        {"image": {"lock-image": "LOKI_IMAGE"}}, MarkerContext(Path(), {"LOKI_IMAGE": LOKI}, "loki.yaml")
    )

    assert is_mapping(resolved)
    image = resolved["image"]
    assert is_mapping(image)
    assert list(image) == ["registry", "repository", "tag", "digest"]


def test_lock_image_marker_supports_the_grafana_sha_spelling() -> None:
    resolved = resolve_markers(
        {"image": {"lock-image": {"key": "LOKI_IMAGE", "digest_field": "sha"}}},
        MarkerContext(Path(), {"LOKI_IMAGE": LOKI}, "grafana.yaml"),
    )

    assert is_mapping(resolved)
    image = resolved["image"]
    assert is_mapping(image)
    assert image["sha"] == DIGEST.removeprefix("sha256:")
    assert "digest" not in image


def test_lock_image_marker_rejects_unknown_digest_fields() -> None:
    with pytest.raises(ValidationError, match=r"(?s).*digest_field"):
        resolve_markers(
            {"image": {"lock-image": {"key": "LOKI_IMAGE", "digest_field": "checksum"}}},
            MarkerContext(Path(), {"LOKI_IMAGE": LOKI}, "grafana.yaml"),
        )


def test_lock_image_marker_rejects_extra_spec_keys() -> None:
    with pytest.raises(ValidationError):
        resolve_markers(
            {"image": {"lock-image": {"key": "LOKI_IMAGE", "pull": "always"}}},
            MarkerContext(Path(), {"LOKI_IMAGE": LOKI}, "grafana.yaml"),
        )


def test_lock_image_marker_rejects_non_name_non_mapping_values() -> None:
    with pytest.raises(ValueError, match="name or a key/digest_field mapping"):
        resolve_markers(
            {"image": {"lock-image": ["LOKI_IMAGE"]}}, MarkerContext(Path(), {"LOKI_IMAGE": LOKI}, "grafana.yaml")
        )


def test_lock_image_marker_splits_untagged_references_without_a_tag_field() -> None:
    untagged = "ghcr.io/outernet-foundation/mirror/docker.io/grafana/loki@" + DIGEST

    resolved = resolve_markers(
        {"image": {"lock-image": "LOKI_IMAGE"}}, MarkerContext(Path(), {"LOKI_IMAGE": untagged}, "loki.yaml")
    )

    assert resolved == {
        "image": {
            "registry": "ghcr.io/outernet-foundation/mirror/docker.io/grafana",
            "repository": "loki",
            "digest": DIGEST,
        }
    }


def test_lock_image_marker_treats_a_port_colon_as_registry_not_tag() -> None:
    ported = "registry.internal:5000/team/app@" + DIGEST

    resolved = resolve_markers(
        {"image": {"lock-image": "APP_IMAGE"}}, MarkerContext(Path(), {"APP_IMAGE": ported}, "app.yaml")
    )

    assert resolved == {
        "image": {
            "registry": "registry.internal:5000/team",
            "repository": "app",
            "digest": DIGEST,
        }
    }


def test_literal_image_values_refuse_naming_the_lock_marker() -> None:
    with pytest.raises(ValueError, match=r"k8s\.yaml.*'ghcr.io/example/app:v1' must consume the lock.*lock: NAME"):
        resolve_markers({"image": "ghcr.io/example/app:v1"}, MarkerContext(Path(), {}, "k8s.yaml"))


def test_untagged_literal_image_values_refuse_anywhere_in_the_tree() -> None:
    with pytest.raises(ValueError, match="must consume the lock"):
        resolve_markers({"containers": [{"image": "postgres"}]}, MarkerContext(Path(), {}, "workload api"))


def test_image_values_with_slots_and_plain_non_image_keys_pass() -> None:
    node: Any = {
        "name": "plain-value",
        "image": "${API_SHA}",
        "runtime": {"image": "${SPIKE_IMAGE:?err}"},
        "templated": {"image": 'ghcr.io/org/api:{{ env "API_SHA" }}'},
    }

    assert resolve_markers(node, MarkerContext(Path(), {}, "x.yaml")) == node


def _provisioner_context(repository: str = "ghcr.io/org/repo", **images: Image) -> MarkerContext:
    return MarkerContext(Path(), {}, "postgres.yaml", repository=repository, builds=images)


def test_dockerfile_marker_composes_the_env_slot_reference() -> None:
    image = Image(name="postgres", dockerfile="workloads/postgres/Dockerfile")

    resolved = resolve_markers(
        {"image": {"dockerfile": "workloads/postgres/Dockerfile"}},
        _provisioner_context(postgres=image),
    )

    assert resolved == {"image": 'ghcr.io/org/repo/postgres:{{ env "POSTGRES_SHA" }}'}


def test_dockerfile_marker_on_an_image_with_an_axis_carries_the_axis_env_slot() -> None:
    image = Image(
        name="reconstructor",
        dockerfile="workloads/reconstructor/Dockerfile",
        axis="TORCH_DEVICE",
        variants=["cuda", "rocm"],
    )

    resolved = resolve_markers(
        {"image": {"dockerfile": "workloads/reconstructor/Dockerfile"}},
        _provisioner_context(reconstructor=image),
    )

    assert resolved == {
        "image": 'ghcr.io/org/repo/reconstructor-{{ env "TORCH_DEVICE" }}:{{ env "RECONSTRUCTOR_SHA" }}'
    }


def test_dockerfile_marker_without_the_provisioner_context_refuses() -> None:
    with pytest.raises(ValueError, match="dockerfile: is a provisioner-manifest marker"):
        resolve_markers(
            {"image": {"dockerfile": "workloads/postgres/Dockerfile"}}, MarkerContext(Path(), {}, "loki.yaml")
        )


def test_dockerfile_marker_naming_no_build_set_image_refuses() -> None:
    with pytest.raises(ValueError, match=r"postgres\.yaml.*names no image in the build set"):
        resolve_markers(
            {"image": {"dockerfile": "workloads/ghost/Dockerfile"}},
            _provisioner_context(postgres=Image(name="postgres", dockerfile="workloads/postgres/Dockerfile")),
        )


def test_dockerfile_marker_with_a_non_string_value_refuses() -> None:
    with pytest.raises(ValueError, match="path string"):
        resolve_markers(
            {"image": {"dockerfile": ["workloads/postgres/Dockerfile"]}},
            _provisioner_context(),
        )


def test_declared_dockerfile_paths_walks_provisioner_entries() -> None:
    entries: Any = [
        {"uri": "template://example/config", "type": "example-config"},
        {"services": {"pf-postgres": {"image": {"dockerfile": "workloads/postgres/Dockerfile"}}}},
        {"manifests": [{"spec": {"containers": [{"image": {"dockerfile": "workloads/migrator/Dockerfile"}}]}}]},
    ]

    assert declared_dockerfile_paths(entries) == [
        "workloads/postgres/Dockerfile",
        "workloads/migrator/Dockerfile",
    ]


def test_declared_dockerfile_paths_leaves_lock_markers_alone() -> None:
    entries: Any = [{"services": {"loki": {"image": {"lock": "LOKI_IMAGE"}}}}]

    assert declared_dockerfile_paths(entries) == []
