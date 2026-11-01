import json

import pytest

from docker_devkit import image_refs
from docker_devkit.image_refs import (
    resolve_remote_digest,
    resolve_remote_manifests,
    select_platform_manifests,
)

FAKE_DIGEST = "sha256:" + "a" * 64
DIGEST_AMD64 = "sha256:" + "c" * 64
DIGEST_ARM64 = "sha256:" + "d" * 64
DIGEST_ATTESTATION = "sha256:" + "e" * 64


def _inspect_output_with_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return f"Name: x\nDigest: {FAKE_DIGEST}\n"


def _inspect_output_without_digest(command: str) -> str:
    assert "imagetools inspect" in command
    return "Name: x\n"


def _child(
    digest: str, os_name: str, architecture: str, annotations: dict[str, str] | None = None
) -> dict[str, object]:
    child: dict[str, object] = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": digest,
        "size": 1234,
        "platform": {"os": os_name, "architecture": architecture},
    }
    if annotations is not None:
        child["annotations"] = annotations
    return child


class TestResolveRemoteDigest:
    def test_should_return_parsed_digest(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_refs, "bash_output", _inspect_output_with_digest)
        assert resolve_remote_digest("docker.io/library/alpine:3.20") == FAKE_DIGEST

    def test_should_raise_when_digest_missing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_refs, "bash_output", _inspect_output_without_digest)
        with pytest.raises(RuntimeError):
            resolve_remote_digest("docker.io/library/alpine:3.20")


class TestSelectPlatformManifests:
    def test_selects_one_manifest_per_declared_platform(self):
        children: list[object] = [
            _child(DIGEST_AMD64, "linux", "amd64"),
            _child(DIGEST_ARM64, "linux", "arm64"),
            _child(DIGEST_ATTESTATION, "unknown", "unknown", {"vnd.docker.reference.type": "attestation-manifest"}),
        ]

        selected = select_platform_manifests(children, ["linux/amd64", "linux/arm64"], "entry X")

        assert selected == {"linux/amd64": DIGEST_AMD64, "linux/arm64": DIGEST_ARM64}

    def test_skips_attestation_entries_carrying_a_concrete_platform(self):
        children: list[object] = [
            _child(DIGEST_AMD64, "linux", "amd64"),
            _child(DIGEST_ATTESTATION, "linux", "amd64", {"vnd.docker.reference.type": "attestation-manifest"}),
        ]

        selected = select_platform_manifests(children, ["linux/amd64"], "entry X")

        assert selected == {"linux/amd64": DIGEST_AMD64}

    def test_ignores_variant_fields_and_platformless_descriptors(self):
        children: list[object] = [
            _child(DIGEST_ARM64, "linux", "arm64"),
            {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "f" * 64},
            "not-a-descriptor",
        ]

        selected = select_platform_manifests(children, ["linux/arm64"], "entry X")

        assert selected == {"linux/arm64": DIGEST_ARM64}

    def test_multiple_matches_refuse_naming_the_entry(self):
        children: list[object] = [_child(DIGEST_AMD64, "linux", "amd64"), _child(DIGEST_ARM64, "linux", "amd64")]

        with pytest.raises(RuntimeError, match=f"entry X.*multiple upstream manifests.*{DIGEST_AMD64}, {DIGEST_ARM64}"):
            select_platform_manifests(children, ["linux/amd64"], "entry X")

    def test_a_declared_platform_no_index_child_covers_refuses_naming_the_entry(self):
        children: list[object] = [_child(DIGEST_AMD64, "linux", "amd64")]

        with pytest.raises(RuntimeError, match=r"entry X.*no linux/arm64 manifest.*index platforms: \['linux/amd64'\]"):
            select_platform_manifests(children, ["linux/arm64"], "entry X")


class RawInspectOutput:
    def __init__(self, output: str) -> None:
        self.output = output

    def __call__(self, command: str) -> str:
        assert "imagetools inspect" in command
        return self.output


def _fixed_digest(reference: str) -> str:
    return FAKE_DIGEST


class TestResolveRemoteManifests:
    def test_resolves_the_index_and_selects_declared_platforms(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [_child(DIGEST_AMD64, "linux", "amd64"), _child(DIGEST_ARM64, "linux", "arm64")],
        })
        monkeypatch.setattr(image_refs, "bash_output", RawInspectOutput(raw))
        monkeypatch.setattr(image_refs, "resolve_remote_digest", _fixed_digest)

        resolved = resolve_remote_manifests("docker.io/library/alpine:3.20", ["linux/arm64"], "entry X")

        assert resolved.index_digest == FAKE_DIGEST
        assert resolved.platforms == {"linux/arm64": DIGEST_ARM64}

    def test_a_manifest_reference_records_uniformly(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {}})
        monkeypatch.setattr(image_refs, "bash_output", RawInspectOutput(raw))
        monkeypatch.setattr(image_refs, "resolve_remote_digest", _fixed_digest)

        resolved = resolve_remote_manifests("docker.io/library/golang:1.24", ["linux/amd64", "linux/arm64"], "entry X")

        assert resolved.index_digest == FAKE_DIGEST
        assert resolved.platforms == {"linux/amd64": FAKE_DIGEST, "linux/arm64": FAKE_DIGEST}
