from __future__ import annotations

import base64
import difflib
import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

import yaml
from bashrun.bash import bash_output
from pydantic import BaseModel, ConfigDict, Field

from boatyard.config.config import (
    OCI_REFERENCE_PREFIX,
    ConfigFile,
    Yaml,
    bundle_lock_key,
    chart_lock_key,
    is_mapping,
    mirrored_ref,
    refuse_unresolved_references,
    required_compose_holes,
)
from boatyard.crane import install_crane
from boatyard.config.lock import (
    LOCK_PATH,
    LockFile,
    ConfigLockBundle,
    ConfigLockEntry,
)
from boatyard.paths import DEFAULT_CONFIG_PATH
from boatyard.temp import SHA256_DIGEST_REGEX
from boatyard.yaml import load_yaml_model

DIGEST_TAIL_PATTERN = re.compile(rf"@{SHA256_DIGEST_REGEX}$")
COMPOSE_FILE_MEDIA_TYPE = "application/vnd.docker.compose.file+yaml"


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


class RelockResult(NamedTuple):
    lock: LockFile
    rendered: str


def run_lock(*, upgrade: bool = False, check: bool = False) -> None:
    config_path = Path.cwd() / DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        print("no boatyard.yml; no lock declarations to relock")
        return
    config = ConfigFile.load(config_path)
    declared_charts = config.declared_charts
    remote_entries = config.remote_declared_entries()
    if not config.base_images and not declared_charts and not remote_entries:
        print("no lock declarations (none of base_images, k8s.charts, service refs); nothing to relock")
        return

    lock_file = config.root / LOCK_PATH
    computed = relock(config, upgrade=upgrade).rendered
    if not check:
        lock_file.write_text(computed, encoding="utf-8")
        return

    committed = lock_file.read_text(encoding="utf-8") if lock_file.exists() else ""
    if committed != computed:
        print(
            "".join(
                difflib.unified_diff(
                    committed.splitlines(keepends=True),
                    computed.splitlines(keepends=True),
                    fromfile=f"committed {LOCK_PATH.as_posix()}",
                    tofile=f"computed {LOCK_PATH.as_posix()}",
                )
            )
        )
        raise ValueError("image lock is stale")


def relock(config: ConfigFile, *, upgrade: bool = False) -> RelockResult:

    prefix = config.mirror_prefix
    # The write path's committed read: drift between the committed lock and the config
    # is relock's input, never refused here.
    committed_path = config.root / LOCK_PATH
    committed = load_yaml_model(LockFile, committed_path) if committed_path.is_file() else LockFile()

    images: dict[str, ConfigLockEntry] = {}
    for name, reference in config.base_images.items():
        base = mirrored_ref(reference, prefix)
        committed_entry = committed.images.get(name)
        if DIGEST_TAIL_PATTERN.search(reference):
            images[name] = ConfigLockEntry(index=base, platforms=dict.fromkeys(config.platforms, base))
        elif (
            not upgrade
            and committed_entry is not None
            and _pinned_to(committed_entry.index, base)
            and sorted(committed_entry.platforms) == sorted(config.platforms)
        ):
            images[name] = committed_entry
        else:
            subject = f"base_images entry {name}"
            install_crane()
            print(f"Resolving digests for: {reference}")
            index = OciIndex.model_validate(json.loads(bash_output(f"crane manifest {shlex.quote(reference)}")))
            index_digest = resolve_remote_digest(reference)
            children = index.manifests
            if not children:
                # Record a manifest answer as the index and every platform entry.
                images[name] = ConfigLockEntry(
                    index=f"{base}@{index_digest}",
                    platforms=dict.fromkeys(config.platforms, f"{base}@{index_digest}"),
                )
            else:
                selected: dict[str, str] = {}
                for platform in config.platforms:
                    matches = [
                        child
                        for child in children
                        if child.annotations.get("vnd.docker.reference.type") != "attestation-manifest"
                        if _concrete_platform(child) == platform
                    ]
                    if len(matches) > 1:
                        digests = ", ".join(_child_digest(child, subject) for child in matches)
                        raise ValueError(
                            f"{subject}: platform {platform} matches multiple upstream manifests ({digests})"
                        )
                    if not matches:
                        available = sorted({
                            concrete for child in children if (concrete := _concrete_platform(child)) is not None
                        })
                        raise ValueError(
                            f"{subject}: the upstream index carries no {platform} manifest "
                            f"(declared platforms: {sorted(config.platforms)}; index platforms: {available})"
                        )
                    selected[platform] = _child_digest(matches[0], subject)
                images[name] = ConfigLockEntry(
                    index=f"{base}@{index_digest}",
                    platforms={platform: f"{base}@{digest}" for platform, digest in selected.items()},
                )

    charts: dict[str, str] = {}
    declared_charts = config.declared_charts
    for chart_name, chart_reference in declared_charts.items():
        key = chart_lock_key(chart_name)
        reference = chart_reference.removeprefix(OCI_REFERENCE_PREFIX)
        base = mirrored_ref(reference, prefix)
        committed_chart = committed.charts.get(key)
        if DIGEST_TAIL_PATTERN.search(reference):
            charts[key] = base
        elif committed_chart is not None and not upgrade and _pinned_to(committed_chart, base):
            charts[key] = committed_chart
        else:
            charts[key] = f"{base}@{resolve_remote_digest(reference)}"

    # First-party bundles resolve direct, never mirror-prefixed. A frozen digest's artifact is
    # immutable, so its recorded requires copies forward; declaring entries re-read the content
    # to validate their authored references against the bundle's actual services.
    bundles: dict[str, ConfigLockBundle] = {}
    for name, declared, entry in config.remote_declared_entries():
        key = bundle_lock_key(name)
        reference = declared
        committed_bundle = committed.bundles.get(key)
        frozen_bundle = (
            committed_bundle
            if committed_bundle is not None and not upgrade and _pinned_to(committed_bundle.reference, reference)
            else None
        )
        if frozen_bundle is not None and not entry.routes and not entry.ports:
            bundles[key] = frozen_bundle
            continue
        pinned = (
            frozen_bundle.reference if frozen_bundle is not None else f"{reference}@{resolve_remote_digest(reference)}"
        )
        # Compose bundles are OCI artifacts whose compose files ride as inline base64 layer data;
        # export cannot read them, the manifest data and the blob fallback can.
        install_crane()
        print(f"Reading bundle content for: {pinned}")
        artifact = OciArtifactManifest.model_validate(json.loads(bash_output(f"crane manifest {shlex.quote(pinned)}")))
        # A colon is a tag only when no slash follows it; earlier colons are registry ports.
        repository = pinned.rpartition("@")[0]
        slash = repository.rfind("/")
        colon = repository.rfind(":")
        repository = repository[:colon] if colon > slash else repository
        texts = [
            base64.b64decode(layer.data).decode("utf-8")
            if layer.data is not None
            else bash_output(f"crane blob {shlex.quote(repository + '@' + layer.digest)}")
            for layer in artifact.layers
            if layer.media_type == COMPOSE_FILE_MEDIA_TYPE
        ]
        if not texts:
            raise ValueError(f"{pinned}: the compose bundle carries no compose file layer")
        bundles[key] = (
            frozen_bundle
            if frozen_bundle is not None
            else ConfigLockBundle(reference=pinned, requires=sorted(required_compose_holes("\n".join(texts))))
        )
        service_names: set[str] = set()
        for text in texts:
            bundle_document = yaml.safe_load(text)
            services = bundle_document.get("services") if is_mapping(bundle_document) else None
            if is_mapping(services):
                service_names.update(services)
        refuse_unresolved_references(name, entry.routes, entry.ports, service_names, prefix=f"{name}-")

    lock = LockFile(platforms=list(config.platforms), images=images, charts=charts, bundles=bundles)
    # Render names and platform keys in sorted order.
    document: dict[str, Yaml] = {}
    if lock.platforms:
        document["platforms"] = list(lock.platforms)
    if lock.images:
        document["images"] = {
            name: {"index": entry.index, "platforms": dict(sorted(entry.platforms.items()))}
            for name, entry in sorted(lock.images.items())
        }
    if lock.charts:
        document["charts"] = dict(sorted(lock.charts.items()))
    if lock.bundles:
        document["bundles"] = {
            name: {
                "reference": bundle.reference,
                **({"requires": list(bundle.requires)} if bundle.requires else {}),
            }
            for name, bundle in sorted(lock.bundles.items())
        }
    if not document:
        return RelockResult(lock, "{}\n")
    return RelockResult(lock, yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=10_000_000))


def resolve_remote_digest(reference: str) -> str:
    install_crane()
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"crane digest {shlex.quote(reference)}").strip()
    if re.fullmatch(SHA256_DIGEST_REGEX, output) is None:
        raise ValueError(f"crane digest output for {reference} must be one sha256 digest")
    return output


def render_env_entries(entries: Mapping[str, str]) -> str:
    return "".join(f"{name}={reference}\n" for name, reference in sorted(entries.items()))


def _concrete_platform(child: OciDescriptor) -> str | None:
    platform = child.platform
    if platform is None or platform.os is None or platform.architecture is None:
        return None
    if platform.os == "unknown" or platform.architecture == "unknown":
        return None
    return f"{platform.os}/{platform.architecture}"


def _child_digest(child: OciDescriptor, subject: str) -> str:
    if not child.digest.startswith("sha256:"):
        raise ValueError(f"{subject}: upstream index child must carry a digest ({child!r})")
    return child.digest


def _pinned_to(committed: str | None, reference: str) -> bool:
    return committed is not None and committed.startswith(reference + "@")
