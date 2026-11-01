from pathlib import Path

import pytest

from docker_devkit import image_refs
from docker_devkit.image_refs import resolve_remote_digest, unpinned_references

FAKE_DIGEST = "sha256:" + "a" * 64


def _inspect_output_with_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return f"Name: x\nDigest: {FAKE_DIGEST}\n"


def _inspect_output_without_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return "Name: x\n"


class TestUnpinnedReferences:
    def test_should_collect_workload_and_score_sources(self, tmp_path: Path):
        workloads_directory = tmp_path / "workloads" / "api"
        workloads_directory.mkdir(parents=True)
        (workloads_directory / "Dockerfile").write_text(
            "FROM --platform=linux/amd64 ghcr.io/library/python AS base\n"
            "COPY --from=ghcr.io/astral-sh/uv /uv /uvx /usr/local/bin/\n"
            "COPY --from=base /x /y\n"
            "RUN pip install x\n",
            encoding="utf-8",
        )
        score_directory = tmp_path / "stack" / "score"
        score_directory.mkdir(parents=True)
        (score_directory / "app.provisioners.yaml").write_text(
            "containers:\n  - name: keycloak\n    image: quay.io/keycloak/keycloak\n",
            encoding="utf-8",
        )
        assert unpinned_references(tmp_path) == [
            "ghcr.io/library/python",
            "ghcr.io/astral-sh/uv",
            "quay.io/keycloak/keycloak",
        ]

    def test_should_collect_legacy_layout_sources(self, tmp_path: Path):
        docker_directory = tmp_path / "docker" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text("FROM ghcr.io/library/python\n", encoding="utf-8")
        score_directory = tmp_path / "score"
        score_directory.mkdir()
        (score_directory / "app.provisioners.yaml").write_text(
            "containers:\n  - name: keycloak\n    image: quay.io/keycloak/keycloak\n",
            encoding="utf-8",
        )
        assert unpinned_references(tmp_path) == [
            "ghcr.io/library/python",
            "quay.io/keycloak/keycloak",
        ]

    def test_should_collect_nested_score_provisioner_directories(self, tmp_path: Path):
        compose_directory = tmp_path / "stack" / "score" / "compose"
        compose_directory.mkdir(parents=True)
        (compose_directory / "seaweedfs.yaml").write_text(
            "services: |\n  seaweedfs:\n    image: chrislusf/seaweedfs\n",
            encoding="utf-8",
        )
        k8s_directory = tmp_path / "stack" / "score" / "k8s"
        k8s_directory.mkdir()
        (k8s_directory / "postgres.yaml").write_text(
            "manifests: |\n  image: docker.io/library/postgres\n",
            encoding="utf-8",
        )
        assert unpinned_references(tmp_path) == [
            "chrislusf/seaweedfs",
            "docker.io/library/postgres",
        ]

    def test_should_return_empty_for_empty_tree(self, tmp_path: Path):
        assert unpinned_references(tmp_path) == []

    def test_should_return_empty_when_everything_is_pinned(self, tmp_path: Path):
        docker_directory = tmp_path / "workloads" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text(
            "FROM ghcr.io/outernet-foundation/mirror/ghcr.io/astral-sh/uv${UV_BASE_DIGEST}\n"
            "FROM base AS dev\n"
            "COPY --from=ghcr.io/outernet-foundation/mirror/ghcr.io/astral-sh/uv${UV_BASE_DIGEST} /uv /uvx /usr/local/bin/\n"
            f"FROM ghcr.io/outernet-foundation/mirror/docker.io/library/python@sha256:{'b' * 64}\n",
            encoding="utf-8",
        )
        assert unpinned_references(tmp_path) == []

    def test_should_flag_slash_refs_without_tag_digest_or_arg(self, tmp_path: Path):
        docker_directory = tmp_path / "workloads" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text(
            "FROM ghcr.io/outernet-foundation/mirror/docker.io/library/caddy\n", encoding="utf-8"
        )
        assert unpinned_references(tmp_path) == ["ghcr.io/outernet-foundation/mirror/docker.io/library/caddy"]

    def test_should_ignore_stage_names_and_arg_only_refs(self, tmp_path: Path):
        docker_directory = tmp_path / "workloads" / "api"
        docker_directory.mkdir(parents=True)
        (docker_directory / "Dockerfile").write_text(
            "FROM neural-networks-base AS dev\nCOPY --from=build /x /y\n", encoding="utf-8"
        )
        assert unpinned_references(tmp_path) == []


class TestResolveRemoteDigest:
    def test_should_return_parsed_digest(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_refs, "bash_output", _inspect_output_with_digest)
        assert resolve_remote_digest("docker.io/library/alpine:3.20") == FAKE_DIGEST

    def test_should_raise_when_digest_missing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_refs, "bash_output", _inspect_output_without_digest)
        with pytest.raises(RuntimeError):
            resolve_remote_digest("docker.io/library/alpine:3.20")
