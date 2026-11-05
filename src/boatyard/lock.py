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
from pydantic import BaseModel, ConfigDict, Field, field_validator

from boatyard.config.config import (
    OCI_REFERENCE_PREFIX,
    ConfigFile,
    Yaml,
    bundle_lock_key,
    chart_lock_key,
    is_list,
    is_mapping,
    mirrored_ref,
    refuse_unresolved_references,
    required_compose_holes,
)
from boatyard.crane import install_crane
from boatyard.config.lock import (
    LOCK_PATH,
    read_config_lock,
    SHA256_DIGEST_REGEX,
    LockFile,
    ConfigLockBundle,
    ConfigLockEntry,
)
from boatyard.config.values import PLATFORM_KEY
from boatyard.paths import DEFAULT_CONFIG_PATH, VALUES_FILE

DIGEST_TAIL_PATTERN = re.compile(rf"@{SHA256_DIGEST_REGEX}$")
DIGEST_PATTERN = re.compile(r"^Digest:\s+(sha256:[a-f0-9]+)", re.MULTILINE)
COMPOSE_FILE_MEDIA_TYPE = "application/vnd.docker.compose.file+yaml"


class UpstreamManifests(NamedTuple):
    index_digest: str
    platforms: dict[str, str]


class OciPlatform(BaseModel):
    model_config = ConfigDict(extra="ignore")

    os: str | None = None
    architecture: str | None = None


class OciDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    digest: str = ""
    media_type: str | None = None
    data: str | None = None
    platform: OciPlatform | None = None
    annotations: dict[str, str] = Field(default_factory=dict)


class OciIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")

    manifests: list[OciDescriptor] = Field(default_factory=list)

    @field_validator("manifests", mode="before")
    @classmethod
    def _drop_non_descriptors(cls, value: Yaml) -> Yaml:
        if value is None:
            return []
        if is_list(value):
            return [child for child in value if is_mapping(child)]
        return value


class OciArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    layers: list[OciDescriptor] = Field(default_factory=list)


def run_lock(*, upgrade: bool = False, check: bool = False) -> None:
    config_path = Path.cwd() / DEFAULT_CONFIG_PATH
    config = ConfigFile.load(config_path) if config_path.is_file() else None
    if config is None:
        print("no boatyard.yml; no lock declarations to relock")
        return
    declared_charts = config.declared_charts
    remote_entries = config.remote_declared_entries()
    if not config.base_images and not declared_charts and not remote_entries:
        print("no lock declarations (none of base_images, k8s.charts, service refs); nothing to relock")
        return

    if not check:
        write_lock(config, upgrade=upgrade)
        return
    computed = render_lock(relock(config, upgrade=upgrade))

    lock_file = config.root / LOCK_PATH
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


def write_lock(config: ConfigFile, *, upgrade: bool = False) -> LockFile:
    lock = relock(config, upgrade=upgrade)

    (config.root / LOCK_PATH).write_text(render_lock(lock), encoding="utf-8")
    return lock


def relock(config: ConfigFile, *, upgrade: bool = False) -> LockFile:

    prefix = config.mirror_prefix
    committed = read_config_lock(config.root)
    remote_entries = config.remote_declared_entries()
    if remote_entries:
        # Bundle refs resolve direct via crane; the installer runs so the pin, not a preexisting
        # binary, decides the version.
        install_crane()

    images: dict[str, ConfigLockEntry] = {}
    for name, reference in config.base_images.items():
        base = mirrored_ref(reference, prefix)
        if DIGEST_TAIL_PATTERN.search(reference):
            images[name] = ConfigLockEntry(index=base, platforms=dict.fromkeys(config.platforms, base))
        elif not upgrade and _declaration_frozen(committed.images.get(name), base, config.platforms):
            images[name] = committed.images[name]
        else:
            resolved = resolve_remote_manifests(reference, config.platforms, f"base_images entry {name}")
            images[name] = ConfigLockEntry(
                index=f"{base}@{resolved.index_digest}",
                platforms={platform: f"{base}@{digest}" for platform, digest in resolved.platforms.items()},
            )

    charts: dict[str, str] = {}
    declared_charts = config.declared_charts
    for chart_name, chart_reference in declared_charts.items():
        key = chart_lock_key(chart_name)
        reference = chart_reference.removeprefix(OCI_REFERENCE_PREFIX)
        base = mirrored_ref(reference, prefix)
        if DIGEST_TAIL_PATTERN.search(reference):
            charts[key] = base
        elif not upgrade and key in committed.charts and committed.charts[key].startswith(base + "@"):
            charts[key] = committed.charts[key]
        else:
            charts[key] = f"{base}@{resolve_remote_digest(reference)}"

    # First-party bundles resolve direct, never mirror-prefixed. A frozen digest's artifact is
    # immutable, so its recorded requires copies forward; declaring entries re-read the content
    # to validate their authored references against the bundle's actual services.
    bundles: dict[str, ConfigLockBundle] = {}
    for name, declared, entry in config.remote_declared_entries():
        key = bundle_lock_key(name)
        reference = declared
        frozen = (
            not upgrade and key in committed.bundles and committed.bundles[key].reference.startswith(reference + "@")
        )
        if frozen and not entry.routes and not entry.ports:
            bundles[key] = committed.bundles[key]
            continue
        pinned = committed.bundles[key].reference if frozen else f"{reference}@{resolve_bundle_digest(reference)}"
        content = read_compose_bundle(pinned)
        bundles[key] = (
            committed.bundles[key]
            if frozen
            else ConfigLockBundle(reference=pinned, requires=sorted(required_compose_holes("\n".join(content))))
        )
        refuse_unresolved_references(name, entry.routes, entry.ports, _bundle_service_names(content), prefix=f"{name}-")

    return LockFile(images=images, charts=charts, bundles=bundles)


def render_lock(lock: LockFile) -> str:
    # Render names and platform keys in sorted order.
    document: dict[str, Yaml] = {}
    if lock.images:
        document["images"] = {
            name: {
                "index": entry.index,
                **({"platforms": dict(sorted(entry.platforms.items()))} if entry.platforms else {}),
            }
            for name, entry in sorted(lock.images.items())
        }
    if lock.charts:
        document["charts"] = dict(sorted(lock.charts.items()))
    if lock.bundles:
        document["bundles"] = {name: _bundle_record(bundle) for name, bundle in sorted(lock.bundles.items())}
    if not document:
        return "{}\n"
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=10_000_000)


def _bundle_record(bundle: ConfigLockBundle) -> Yaml:
    record: dict[str, Yaml] = {"reference": bundle.reference}
    if bundle.requires:
        requires: list[Yaml] = list(bundle.requires)
        record["requires"] = requires
    return record


def resolve_remote_manifests(reference: str, platforms: list[str], subject: str) -> UpstreamManifests:
    print(f"Resolving digests for: {reference}")
    raw = bash_output(f"docker buildx imagetools inspect --raw {shlex.quote(reference)}")
    index = OciIndex.model_validate(json.loads(raw))
    index_digest = resolve_remote_digest(reference)
    children = index.manifests
    if not children:
        # Record a manifest answer as the index and every platform entry.
        return UpstreamManifests(index_digest, dict.fromkeys(platforms, index_digest))
    selected: dict[str, str] = {}
    for platform in platforms:
        os_name, _, architecture = platform.partition("/")
        matches = [
            child
            for child in children
            if child.annotations.get("vnd.docker.reference.type") != "attestation-manifest"
            if _concrete_platform(child) == (os_name, architecture)
        ]
        if len(matches) > 1:
            digests = ", ".join(_child_digest(child, subject) for child in matches)
            raise ValueError(f"{subject}: platform {platform} matches multiple upstream manifests ({digests})")
        if not matches:
            available = sorted({
                f"{os_name}/{architecture}"
                for child in children
                if (concrete := _concrete_platform(child)) is not None
                for os_name, architecture in (concrete,)
            })
            raise ValueError(
                f"{subject}: the upstream index carries no {platform} manifest "
                f"(declared platforms: {sorted(platforms)}; index platforms: {available})"
            )
        selected[platform] = _child_digest(matches[0], subject)
    return UpstreamManifests(index_digest, selected)


def resolve_remote_digest(reference: str) -> str:
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"docker buildx imagetools inspect {shlex.quote(reference)}")
    match = DIGEST_PATTERN.search(output)
    if match is None:
        raise ValueError(f"imagetools output for {reference} must carry a Digest line")
    return match.group(1)


def resolve_bundle_digest(reference: str) -> str:
    # Compose-bundle media types are not image indexes; imagetools may refuse them, crane does not.
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"crane digest {shlex.quote(reference)}").strip()
    if re.fullmatch(SHA256_DIGEST_REGEX, output) is None:
        raise ValueError(f"crane digest output for {reference} must be one sha256 digest")
    return output


def read_compose_bundle(reference: str) -> list[str]:
    # Compose bundles are OCI artifacts whose compose files ride as inline base64 layer data;
    # export cannot read them, the manifest data and the blob fallback can.
    print(f"Reading bundle content for: {reference}")
    manifest = OciArtifactManifest.model_validate(json.loads(bash_output(f"crane manifest {shlex.quote(reference)}")))
    texts = [
        base64.b64decode(layer.data).decode("utf-8")
        if layer.data is not None
        else bash_output(f"crane blob {shlex.quote(_blob_repository(reference) + '@' + layer.digest)}")
        for layer in manifest.layers
        if layer.media_type == COMPOSE_FILE_MEDIA_TYPE
    ]
    if not texts:
        raise ValueError(f"{reference}: the compose bundle carries no compose file layer")
    return texts


def _blob_repository(reference: str) -> str:
    # A colon is a tag only when no slash follows it; earlier colons are registry ports.
    base = reference.rpartition("@")[0]
    slash = base.rfind("/")
    colon = base.rfind(":")
    return base[:colon] if colon > slash else base


def _bundle_service_names(texts: list[str]) -> set[str]:
    names: set[str] = set()
    for text in texts:
        document = yaml.safe_load(text)
        services = document.get("services") if is_mapping(document) else None
        if is_mapping(services):
            names.update(services)
    return names


def _concrete_platform(child: OciDescriptor) -> tuple[str, str] | None:
    platform = child.platform
    if platform is None or platform.os is None or platform.architecture is None:
        return None
    if platform.os == "unknown" or platform.architecture == "unknown":
        return None
    return (platform.os, platform.architecture)


def _child_digest(child: OciDescriptor, subject: str) -> str:
    if not child.digest.startswith("sha256:"):
        raise ValueError(f"{subject}: upstream index child must carry a digest ({child!r})")
    return child.digest


def compose_env_entries(lock: LockFile, config: ConfigFile, platform: str | None) -> dict[str, str]:
    if platform is None:
        if not config.platforms:
            resolved: str | None = None
        elif len(config.platforms) == 1:
            resolved = config.platforms[0]
        else:
            raise ValueError(
                f"platforms {config.platforms} are declared; {PLATFORM_KEY}: must be named in {VALUES_FILE.name}"
            )
    elif config.platforms and platform not in config.platforms:
        raise ValueError(f"{PLATFORM_KEY}: {platform!r} must be one of the declared platforms {config.platforms}")
    else:
        # A platforms-less repo has nothing of its own to validate the platform against; the
        # producer lock's recorded platforms police it instead.
        resolved = platform
    if resolved is None and lock.images:
        raise ValueError(f"{LOCK_PATH.as_posix()}: records image entries but boatyard.yml declares no platforms")
    return env_entries(lock, resolved)


def producer_env_entries(lock: LockFile, platform: str | None) -> dict[str, str]:
    # A foreign lock has no consumer-side config; its own recorded platforms are the declaration.
    recorded = {platform_name for entry in lock.images.values() for platform_name in entry.platforms}
    if platform is not None:
        resolved = platform
    elif len(recorded) == 1:
        resolved = next(iter(recorded))
    elif not recorded:
        resolved = None
    else:
        raise ValueError(f"the producer lock records platforms {sorted(recorded)}; {PLATFORM_KEY}: must name one")
    return env_entries(lock, resolved)


def env_entries(lock: LockFile, platform: str | None = None) -> dict[str, str]:
    images: dict[str, str] = {}
    for name, entry in lock.images.items():
        if platform is None:
            images[name] = entry.index
            continue
        recorded = entry.platforms.get(platform)
        if recorded is None:
            raise ValueError(
                f"{LOCK_PATH.as_posix()}: image entry {name} records no {platform} platform manifest; "
                f"recorded platforms: {sorted(entry.platforms) or 'none'}"
            )
        images[name] = recorded
    return {**images, **lock.charts}


def render_env_entries(entries: Mapping[str, str]) -> str:
    return "".join(f"{name}={reference}\n" for name, reference in sorted(entries.items()))


def render_merged_env(values: Mapping[str, str], entries: Mapping[str, str]) -> str:
    shared = sorted(set(values) & set(entries))
    if shared:
        raise ValueError(
            f"{VALUES_FILE.name} sets {shared}, which the lock env file also fills; "
            f"the lock is their only source — remove them from {VALUES_FILE.name}"
        )
    document = ""
    if values:
        document += f"# {VALUES_FILE.name}\n{render_env_entries(values)}"
    if entries:
        document += f"# {LOCK_PATH.name}\n{render_env_entries(entries)}"
    return document


def _declaration_frozen(entry: ConfigLockEntry | None, base: str, platforms: list[str]) -> bool:
    if entry is None:
        return False
    return entry.index.startswith(base + "@") and sorted(entry.platforms) == sorted(platforms)
