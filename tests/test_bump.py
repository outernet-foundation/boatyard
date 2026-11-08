import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from boatyard.models.k8s_pins import parse_pins
from boatyard.models.image import Image
from boatyard.paths import K8S_PINS_PATH
from boatyard.verbs import bump as bump_verb


HEAD = "a" * 40
OTHER_HEAD = "b" * 40
API_TAG = "tree-" + "c" * 40
WORKER_TAG = "tree-" + "d" * 40
PINS_HEADER = "# Deploy record written by 'boatyard bump' — the sole writer; never hand-edit\n"


class GitStub:
    def __init__(self, status: str = "") -> None:
        self.status = status

    def __call__(self, command: str, **_kwargs: object) -> str:
        if command == "git rev-parse HEAD":
            return HEAD + "\n"
        if command == "git status --porcelain":
            return self.status
        raise AssertionError(f"unexpected command {command!r}")


def _shard(target: str, tags: list[str]) -> dict[str, object]:
    return {target: {"ref": f"ghcr.io/org/repo/{target}", "digest": "sha256:" + "e" * 64, "tags": tags}}


class BumpHarness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        shards: dict[str, dict[str, object]] | None = None,
        registry: bool = True,
        image_shas: dict[str, str] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.generated: dict[str, object] = {}
        self.pulled: list[str] = []
        self.git = GitStub()
        self.image_shas = image_shas if image_shas is not None else {"WORKER_SHA": WORKER_TAG, "API_SHA": API_TAG}
        resolved_shards = (
            shards
            if shards is not None
            else {
                f"sha-{HEAD}-11111111": _shard("api", ["latest", API_TAG]),
                f"sha-{HEAD}-22222222": _shard("worker", ["latest", WORKER_TAG]),
                f"sha-{OTHER_HEAD}-99999999": _shard("stray", ["latest"]),
            }
        )
        (tmp_path / "boatyard.yml").write_text(
            "requires: '>=0'\nproject_name: example\nrepository: ghcr.io/org/repo\n"
            "services:\n  ext:\n    ref: oci://ghcr.io/org/ext:0.1.0\n"
            "k8s:\n  workloads:\n    - api.yaml\n",
            encoding="utf-8",
        )
        if self.image_shas:
            for directory in ("api", "worker"):
                image_directory = tmp_path / "workloads" / directory
                image_directory.mkdir(parents=True, exist_ok=True)
                (image_directory / "Dockerfile").write_text("ARG PY_IMAGE\nFROM ${PY_IMAGE}\n", encoding="utf-8")

        def _fake_pull(
            registry_address: str, project: str, platform: str, tag: str, target: Path, **_kwargs: object
        ) -> bool:
            self.pulled.append(tag)
            target.mkdir(parents=True, exist_ok=True)
            (target / "images-digests.json").write_text(json.dumps(resolved_shards.get(tag, {})), encoding="utf-8")
            return True

        def _fake_generate(config: object, **kwargs: object) -> list[Path]:
            self.generated.update(kwargs)
            assert parse_pins(yaml.safe_load(self.pins_text())) == self.image_shas
            return []

        def _shelf_tags(registry: str, project: str, platform: str, **kwargs: object) -> list[str]:
            return sorted(resolved_shards)

        def _image_sha(inner_self: Image, _root: Path) -> str:
            return self.image_shas[inner_self.sha_env]

        if registry:
            monkeypatch.setenv("REGISTRY", "ghcr.io/org/builds")
        else:
            monkeypatch.delenv("REGISTRY", raising=False)
        monkeypatch.setattr(bump_verb, "bash_output", self.git)
        monkeypatch.setattr(bump_verb, "install_oras", lambda: None)
        monkeypatch.setattr(bump_verb, "list_build_tags", _shelf_tags)
        monkeypatch.setattr(bump_verb, "pull_artifact", _fake_pull)
        monkeypatch.setattr(Image, "compute_sha", _image_sha)
        monkeypatch.setattr(bump_verb, "generate_artifacts", _fake_generate)
        monkeypatch.chdir(tmp_path)

    def run(self) -> None:
        bump_verb.bump()

    def pins_text(self) -> str:
        return (self.tmp_path / K8S_PINS_PATH).read_text(encoding="utf-8")


def test_bump_writes_certified_pins_and_regenerates_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = BumpHarness(monkeypatch, tmp_path)

    harness.run()

    assert harness.pins_text() == PINS_HEADER + f"API_SHA: {API_TAG}\nWORKER_SHA: {WORKER_TAG}\n"
    assert harness.pulled == [f"sha-{HEAD}-11111111", f"sha-{HEAD}-22222222"]
    assert harness.generated == {"target": "k8s"}


def test_bump_self_skips_where_no_k8s_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "boatyard.yml").write_text(
        "requires: '>=0'\nproject_name: example\nservices:\n  ext:\n    ref: oci://ghcr.io/org/ext:0.1.0\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    bump_verb.bump()

    assert not (tmp_path / K8S_PINS_PATH).exists()


def test_bump_refuses_a_dirty_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BumpHarness(monkeypatch, tmp_path)
    harness.git.status = " M stack/score/api.yaml\n"

    with pytest.raises(ValueError, match="dirty"):
        harness.run()

    assert not (tmp_path / K8S_PINS_PATH).exists()


def test_bump_tolerates_the_pins_file_as_the_only_dirt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BumpHarness(monkeypatch, tmp_path)
    harness.git.status = "?? boatyard.k8s-pins.yml\n"

    harness.run()

    assert harness.pins_text() == PINS_HEADER + f"API_SHA: {API_TAG}\nWORKER_SHA: {WORKER_TAG}\n"


def test_bump_refuses_where_ci_shelved_nothing_at_the_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BumpHarness(monkeypatch, tmp_path, shards={f"sha-{OTHER_HEAD}-99999999": _shard("stray", ["latest"])})

    with pytest.raises(ValueError, match="nothing to pin"):
        harness.run()

    assert not (tmp_path / K8S_PINS_PATH).exists()


def test_bump_refuses_a_tree_tag_no_ci_image_carries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BumpHarness(monkeypatch, tmp_path, shards={f"sha-{HEAD}-11111111": _shard("api", ["latest"])})

    with pytest.raises(ValueError, match="nothing to pin"):
        harness.run()


def test_bump_refuses_a_repeated_target_across_shards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BumpHarness(
        monkeypatch,
        tmp_path,
        shards={
            f"sha-{HEAD}-11111111": _shard("api", ["latest", API_TAG]),
            f"sha-{HEAD}-22222222": _shard("api", ["latest"]),
        },
    )

    with pytest.raises(ValueError, match="repeats build target 'api'"):
        harness.run()


def test_bump_accepts_the_bare_pre_shard_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BumpHarness(
        monkeypatch,
        tmp_path,
        shards={f"sha-{HEAD}": {**_shard("api", ["latest", API_TAG]), **_shard("worker", ["latest", WORKER_TAG])}},
    )

    harness.run()

    assert harness.pulled == [f"sha-{HEAD}"]
    assert harness.pins_text() == PINS_HEADER + f"API_SHA: {API_TAG}\nWORKER_SHA: {WORKER_TAG}\n"


def test_bump_requires_registry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BumpHarness(monkeypatch, tmp_path, registry=False)

    with pytest.raises(ValueError, match="REGISTRY not set"):
        harness.run()


def test_bump_is_idempotent_per_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BumpHarness(monkeypatch, tmp_path)

    harness.run()
    first = harness.pins_text()

    harness.run()

    assert harness.pins_text() == first


def test_bump_writes_the_empty_record_for_a_shaless_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BumpHarness(monkeypatch, tmp_path, image_shas={})

    harness.run()

    assert harness.pins_text() == PINS_HEADER + "{}\n"


def test_pins_refuse_a_non_tree_tag_value() -> None:
    with pytest.raises(ValidationError, match="tree-<oid>"):
        parse_pins({"API_SHA": "latest"})


def test_pins_refuse_a_non_sha_slot_name() -> None:
    with pytest.raises(ValidationError, match=r"\*_SHA"):
        parse_pins({"API_IMAGE": API_TAG})


def test_an_empty_pins_declaration_loads_as_the_empty_record() -> None:
    assert parse_pins(None) == {}
