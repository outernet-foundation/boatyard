from __future__ import annotations

import base64
import json
import re
import shlex
from collections.abc import Mapping

import yaml
from bashrun.bash import bash_output
from pydantic import BaseModel, ConfigDict, Field

from boatyard.config.config import (
    OCI_REFERENCE_PREFIX,
    ConfigFile,
    Yaml,
    bundle_lock_key,
    chart_lock_key,
    is_list,
    is_mapping,
    mirrored_ref,
    scan_bundle_texts,
)
from boatyard.crane import install_crane
from boatyard.config.lock import (
    LOCK_PATH,
    LockFile,
    ConfigLockBundle,
    ConfigLockEntry,
)
from boatyard.temp import SHA256_DIGEST_REGEX
from boatyard.yaml import load_yaml_model

DIGEST_TAIL_PATTERN = re.compile(rf"@{SHA256_DIGEST_REGEX}$")
COMPOSE_FILE_MEDIA_TYPE = "application/vnd.docker.compose.file+yaml"


# Model the OCI shapes crane answers with: index children and artifact layers.
class OciPlatform(BaseModel):
    model_config = ConfigDict(extra="ignore")

    os: str | None = None
    architecture: str | None = None


class OciDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    digest: str
    media_type: str | None = Field(default=None, alias="mediaType")
    data: str | None = None
    platform: OciPlatform | None = None
    annotations: dict[str, str] = Field(default_factory=dict)


class OciIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")

    manifests: list[OciDescriptor] = Field(default_factory=list)


class OciArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    layers: list[OciDescriptor] = Field(default_factory=list)

    @classmethod
    def fetch_compose_texts(cls, pinned: str) -> list[str]:
        # Read the bundle's compose files as inline base64 layer data, with crane blob as the fallback.
        print(f"Reading bundle content for: {pinned}")
        artifact = cls.model_validate(json.loads(bash_output(f"crane manifest {shlex.quote(pinned)}")))

        # Strip the tag, treating a colon as a tag only when no slash follows it.
        repository = pinned.rpartition("@")[0]
        slash = repository.rfind("/")
        colon = repository.rfind(":")
        repository = repository[:colon] if colon > slash else repository
        compose_texts = [
            base64.b64decode(layer.data).decode("utf-8")
            if layer.data is not None
            else bash_output(f"crane blob {shlex.quote(repository + '@' + layer.digest)}")
            for layer in artifact.layers
            if layer.media_type == COMPOSE_FILE_MEDIA_TYPE
        ]

        # Refuse a bundle that carries no compose file layer.
        if not compose_texts:
            raise ValueError(f"{pinned}: the compose bundle carries no compose file layer")
        return compose_texts


def relock(config: ConfigFile, *, upgrade: bool = False, check: bool = False) -> LockFile:
    # Install the pinned crane and derive the mirror prefix.
    install_crane()
    prefix = config.mirror_prefix
    # Read the committed lock schema-only; drift is relock's input, never refused.
    committed_path = config.root / LOCK_PATH
    committed = load_yaml_model(LockFile, committed_path) if committed_path.is_file() else LockFile()

    # Resolve first-party bundles direct, recomputing requires from the pinned content and validating declared references against it.
    bundles: dict[str, ConfigLockBundle] = {}
    for name, reference, entry in config.remote_declared_entries():
        key = bundle_lock_key(name)
        committed_entry = committed.bundles.get(key)

        # Pin the frozen reference or resolve the declaration's fresh digest.
        pinned = (
            committed_entry.reference
            if committed_entry is not None and not upgrade and committed_entry.reference.startswith(reference + "@")
            else f"{reference}@{resolve_remote_digest(reference)}"
        )

        # Record the pinned reference with its required holes.
        bundles[key] = ConfigLockBundle(
            reference=pinned,
            requires=scan_bundle_texts(
                name, entry.routes, entry.ports, OciArtifactManifest.fetch_compose_texts(pinned)
            ).requires,
        )

    images: dict[str, ConfigLockEntry] = {}
    for name, reference in config.base_images.items():
        base = mirrored_ref(reference, prefix)
        committed_entry = committed.images.get(name)

        # Record a digest-pinned declaration as-is under the mirror prefix.
        if DIGEST_TAIL_PATTERN.search(reference):
            images[name] = ConfigLockEntry(index=base, platforms=dict.fromkeys(config.platforms, base))
            continue

        # Freeze the committed entry forward while its pinned tag and platform set hold.
        if (
            committed_entry is not None
            and not upgrade
            and committed_entry.index.startswith(base + "@")
            and set(committed_entry.platforms) == set(config.platforms)
        ):
            images[name] = committed_entry
            continue

        # Resolve the reference into an index digest and its child manifests.
        print(f"Resolving digests for: {reference}")
        index_digest = resolve_remote_digest(reference)
        children = OciIndex.model_validate(
            json.loads(bash_output(f"crane manifest {shlex.quote(reference)}"))
        ).manifests

        if not children:
            # Treat a manifest answer as every platform's sole manifest.
            matches = {platform: [OciDescriptor(digest=index_digest)] for platform in config.platforms}
        else:
            # Match each declared platform against its concrete, non-attestation children.
            matches = {
                platform: [
                    child
                    for child in children
                    if child.annotations.get("vnd.docker.reference.type") != "attestation-manifest"
                    if child.platform is not None and f"{child.platform.os}/{child.platform.architecture}" == platform
                ]
                for platform in config.platforms
            }

        # Refuse any platform without exactly one match and record the survivor's digest.
        selected: dict[str, str] = {}
        for platform in config.platforms:
            if len(matches[platform]) != 1:
                raise ValueError(
                    f"base_images entry {name}: platform {platform} "
                    f"matches {len(matches[platform])} upstream manifests, expected exactly one"
                )
            selected[platform] = _child_digest(matches[platform][0], f"base_images entry {name}")

        # Record the mirror-prefixed index and one reference per platform.
        images[name] = ConfigLockEntry(
            index=f"{base}@{index_digest}",
            platforms={platform: f"{base}@{digest}" for platform, digest in selected.items()},
        )

    # Freeze unchanged charts and resolve fresh tags to mirror-pinned digests.
    charts: dict[str, str] = {}
    for name, reference in config.declared_charts.items():
        key = chart_lock_key(name)
        reference = reference.removeprefix(OCI_REFERENCE_PREFIX)
        base = mirrored_ref(reference, prefix)
        committed_entry = committed.charts.get(key)

        # Record a digest-pinned declaration as-is under the mirror prefix.
        if DIGEST_TAIL_PATTERN.search(reference):
            charts[key] = base
            continue

        # Freeze the committed entry forward while its pinned tag holds.
        if committed_entry is not None and not upgrade and committed_entry.startswith(base + "@"):
            charts[key] = committed_entry
            continue

        charts[key] = f"{base}@{resolve_remote_digest(reference)}"

    # Assemble the lock and render its canonical form: sections in field order, nested keys sorted.
    lock = LockFile(platforms=list(config.platforms), images=images, charts=charts, bundles=bundles)
    document = {key: _canonical(value) for key, value in lock.model_dump(exclude_defaults=True).items()}
    rendered = (
        yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=10_000_000) if document else "{}\n"
    )
    # Write the rendered lock over the committed file.
    if not check:
        committed_path.write_text(rendered, encoding="utf-8")
        return lock
    # Refuse staleness when the committed lock is not the computed canonical bytes.
    committed_text = committed_path.read_text(encoding="utf-8") if committed_path.is_file() else ""
    if committed_text != rendered:
        raise SystemExit("image lock is stale. Run 'boatyard lock' and commit the result.")
    return lock


def resolve_remote_digest(reference: str) -> str:
    # Refuse crane digest output that is not one sha256 digest.
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"crane digest {shlex.quote(reference)}").strip()
    if re.fullmatch(SHA256_DIGEST_REGEX, output) is None:
        raise ValueError(f"crane digest output for {reference} must be one sha256 digest")
    return output


def render_env_entries(entries: Mapping[str, str]) -> str:
    # Render the entries as sorted dotenv lines.
    return "".join(f"{name}={reference}\n" for name, reference in sorted(entries.items()))


def _child_digest(child: OciDescriptor, subject: str) -> str:
    # Return the child's sha256 digest, refusing anything else.
    if not child.digest.startswith("sha256:"):
        raise ValueError(f"{subject}: upstream index child must carry a digest ({child!r})")
    return child.digest


def _canonical(value: Yaml) -> Yaml:
    # Return the value with every nested dict's keys sorted.
    if is_mapping(value):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if is_list(value):
        return [_canonical(item) for item in value]
    return value
