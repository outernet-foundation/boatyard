from __future__ import annotations

import base64
import json
import re
import shlex
from typing import NamedTuple

import yaml
from bashrun.bash import bash_output
from pydantic import BaseModel, ConfigDict, Field

from boatyard.models.config_file import (
    GATEWAY_SERVICE_NAME,
    OCI_REFERENCE_PREFIX,
    PublishedPort,
    Route,
    ConfigFile,
    artifact_service_names,
    artifact_slots,
    bundle_lock_key,
    chart_lock_key,
    refuse_unresolved_references,
)
from boatyard.crane import install_crane
from boatyard.models.lock_file import (
    LOCK_PATH,
    LockFile,
    ConfigLockBundle,
    ConfigLockEntry,
    refuse_local_lock_entries,
)
from boatyard.models.workload import (
    AUTOSTART_ANNOTATION,
    PUBLISH_PORT_LABEL,
    ROUTE_PATH_LABEL,
    ROUTE_PATH_PATTERN,
    ROUTE_PORT_LABEL,
    ROUTE_STRIP_LABEL,
    STANZA_LABELS,
)
from boatyard.temp import SHA256_DIGEST_REGEX
from boatyard.yaml import Yaml, is_list, is_mapping, load_yaml_model

DIGEST_TAIL_PATTERN = re.compile(rf"@{SHA256_DIGEST_REGEX}$")
COMPOSE_FILE_MEDIA_TYPE = "application/vnd.docker.compose.file+yaml"


# Model the OCI shapes crane answers with: index children and artifact layers
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
        # Read the bundle's compose files from the artifact's layers
        print(f"Reading bundle content for: {pinned}")
        artifact = cls.model_validate(json.loads(bash_output(f"crane manifest {shlex.quote(pinned)}")))

        # Strip the tag only when no slash follows the colon
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

        # Refuse a bundle that carries no compose file layer
        if not compose_texts:
            raise ValueError(f"{pinned}: the compose bundle carries no compose file layer")
        return compose_texts


class BundleRecords(NamedTuple):
    services: list[str]
    routes: list[Route]
    ports: list[PublishedPort]
    container_names: list[str]
    held_services: list[str]
    declared_ports: dict[str, list[tuple[int, int, str]]]


def relock(config: ConfigFile, *, upgrade: bool = False, check: bool = False) -> LockFile:
    # Install the pinned crane
    install_crane()

    # Read the committed lock schema-only
    committed_path = config.root / LOCK_PATH
    committed = load_yaml_model(LockFile, committed_path) if committed_path.is_file() else LockFile()

    # Resolve the declared remote bundles direct
    bundles: dict[str, ConfigLockBundle] = {}
    for name, reference, entry in config.remote_declared_entries():
        key = bundle_lock_key(name)
        committed_entry = committed.bundles.get(key)

        # Pin the frozen reference or resolve a fresh digest
        pinned = (
            committed_entry.reference
            if committed_entry is not None and not upgrade and committed_entry.reference.startswith(reference + "@")
            else f"{reference}@{resolve_remote_digest(reference)}"
        )

        # Record the bundle's lock entry
        texts = OciArtifactManifest.fetch_compose_texts(pinned)
        records = extract_bundle_records(texts)
        refuse_unresolved_references(name, records.routes, entry.ports, artifact_service_names(texts))
        if GATEWAY_SERVICE_NAME in records.services:
            raise ValueError(f"{pinned}: the included bundle defines the reserved service {GATEWAY_SERVICE_NAME!r}")
        bundles[key] = ConfigLockBundle(
            reference=pinned,
            slots=artifact_slots(texts),
            services=records.services,
            routes=records.routes,
            ports=records.ports,
            container_names=records.container_names,
            held_services=records.held_services,
            declared_ports=records.declared_ports,
        )

    # Refuse committed entries for local base_images keys
    images: dict[str, ConfigLockEntry] = {}
    refuse_local_lock_entries(committed, config, committed_path.as_posix())

    # Resolve each remote base image at its upstream spelling
    for name, reference in config.remote_base_images.items():
        committed_entry = committed.images.get(name)

        # Record a digest-pinned declaration as-is
        if DIGEST_TAIL_PATTERN.search(reference):
            images[name] = ConfigLockEntry(index=reference, platforms=dict.fromkeys(config.platforms, reference))
            continue

        # If the committed pin still matches the tag and platforms, freeze it forward
        if (
            committed_entry is not None
            and not upgrade
            and committed_entry.index.startswith(reference + "@")
            and set(committed_entry.platforms) == set(config.platforms)
        ):
            images[name] = committed_entry
            continue

        # Resolve the reference into its index and child manifests
        print(f"Resolving digests for: {reference}")
        index_digest = resolve_remote_digest(reference)
        children = OciIndex.model_validate(
            json.loads(bash_output(f"crane manifest {shlex.quote(reference)}"))
        ).manifests

        # If the answer is a plain manifest, it is every platform's sole manifest
        if not children:
            matches = {platform: [OciDescriptor(digest=index_digest)] for platform in config.platforms}
        else:
            # Match each platform against its concrete, non-attestation children
            matches = {
                platform: [
                    child
                    for child in children
                    if child.annotations.get("vnd.docker.reference.type") != "attestation-manifest"
                    if child.platform is not None and f"{child.platform.os}/{child.platform.architecture}" == platform
                ]
                for platform in config.platforms
            }

        # Refuse any platform without exactly one match
        selected: dict[str, str] = {}
        for platform in config.platforms:
            if len(matches[platform]) != 1:
                raise ValueError(
                    f"image pin {name}: platform {platform} "
                    f"matches {len(matches[platform])} upstream manifests, expected exactly one"
                )
            child = matches[platform][0]
            if not child.digest.startswith("sha256:"):
                raise ValueError(f"image pin {name}: upstream index child must carry a digest ({child!r})")
            selected[platform] = child.digest

        # Record the index and one reference per platform
        images[name] = ConfigLockEntry(
            index=f"{reference}@{index_digest}",
            platforms={platform: f"{reference}@{digest}" for platform, digest in selected.items()},
        )

    # Freeze or resolve each declared chart
    charts: dict[str, str] = {}
    for name, reference in config.declared_charts.items():
        key = chart_lock_key(name)
        reference = reference.removeprefix(OCI_REFERENCE_PREFIX)
        committed_entry = committed.charts.get(key)

        # Record a digest-pinned declaration as-is
        if DIGEST_TAIL_PATTERN.search(reference):
            charts[key] = reference
            continue

        # If the committed pin still matches the tag, freeze it forward
        if committed_entry is not None and not upgrade and committed_entry.startswith(reference + "@"):
            charts[key] = committed_entry
            continue

        charts[key] = f"{reference}@{resolve_remote_digest(reference)}"

    # Assemble the lock and render its canonical form
    lock = LockFile(platforms=list(config.platforms), images=images, charts=charts, bundles=bundles)
    document = {key: _canonical(value) for key, value in lock.model_dump(exclude_defaults=True).items()}
    rendered = (
        yaml.safe_dump(document, sort_keys=False, default_flow_style=False, width=10_000_000) if document else "{}\n"
    )
    # If not checking, write the rendered lock over the committed file
    if not check:
        committed_path.write_text(rendered, encoding="utf-8")
        return lock

    # If the committed lock is not the canonical bytes, refuse staleness
    committed_text = committed_path.read_text(encoding="utf-8") if committed_path.is_file() else ""
    if committed_text != rendered:
        raise SystemExit("image lock is stale. Run 'boatyard lock' and commit the result.")
    return lock


def resolve_remote_digest(reference: str) -> str:
    # Refuse crane digest output that is not one sha256 digest
    print(f"Resolving digest for: {reference}")
    output = bash_output(f"crane digest {shlex.quote(reference)}").strip()
    if re.fullmatch(SHA256_DIGEST_REGEX, output) is None:
        raise ValueError(f"crane digest output for {reference} must be one sha256 digest")
    return output


def extract_bundle_records(texts: list[str]) -> BundleRecords:
    # Collect every stanza before any reference is checked
    services: list[str] = []
    container_names: set[str] = set()
    held_services: set[str] = set()
    declared_ports: dict[str, list[tuple[int, int, str]]] = {}
    stanzas: list[tuple[str, Yaml, dict[str, str]]] = []
    for text in texts:
        document = yaml.safe_load(text)
        service_map = document.get("services") if is_mapping(document) else None
        if not is_mapping(service_map):
            continue
        for service_name, stanza in service_map.items():
            if service_name not in services:
                services.append(service_name)
            if not is_mapping(stanza):
                stanzas.append((service_name, stanza, {}))
                continue
            if "container_name" in stanza:
                container_names.add(service_name)
            _collect_declared_ports(declared_ports, service_name, stanza.get("ports"))
            stanzas.append((service_name, stanza, stanza_annotations(stanza)))

    # Read the routes and ports from the interchange labels
    routes: list[Route] = []
    ports: list[PublishedPort] = []
    route_owners: dict[str, str] = {}
    for service_name, _stanza, service_annotations in stanzas:
        if service_annotations.get(AUTOSTART_ANNOTATION) == "false":
            held_services.add(service_name)
        unknown = sorted(key for key in service_annotations if key.startswith("boatyard/") and key not in STANZA_LABELS)
        if unknown:
            raise ValueError(
                f"the service {service_name!r} carries the unknown label {unknown[0]!r}; the boatyard/* "
                f"stanza labels are {list(STANZA_LABELS)}"
            )
        route_path = service_annotations.get(ROUTE_PATH_LABEL)
        if route_path is not None:
            if ROUTE_PATH_PATTERN.fullmatch(route_path) is None:
                raise ValueError(
                    f"{ROUTE_PATH_LABEL} {route_path!r} must be a /-rooted segment chain, optionally wildcarded as /*"
                )
            route_port = service_annotations.get(ROUTE_PORT_LABEL)
            if route_port is None:
                raise ValueError(
                    f"{ROUTE_PATH_LABEL}: the claim on the service {service_name!r} carries no {ROUTE_PORT_LABEL} label"
                )
            strip_value = service_annotations.get(ROUTE_STRIP_LABEL)
            if strip_value is not None and strip_value not in ("true", "false"):
                raise ValueError(f"{ROUTE_STRIP_LABEL} {strip_value!r} must spell 'true' or 'false'")
            if route_path in route_owners:
                raise ValueError(
                    f"{ROUTE_PATH_LABEL}: the path {route_path} is claimed by both {route_owners[route_path]} "
                    f"and {service_name}"
                )
            route_owners[route_path] = service_name
            if not route_port.isdigit() or not 1 <= int(route_port) <= 65535:
                raise ValueError(f"{ROUTE_PORT_LABEL} {route_port!r} must be a literal port in the range 1-65535")
            routes.append(
                Route(
                    path=route_path,
                    upstream=f"{service_name}:{int(route_port)}",
                    strip_prefix=strip_value == "true",
                )
            )
        publish_value = service_annotations.get(PUBLISH_PORT_LABEL)
        if publish_value is not None:
            for entry in publish_value.split(","):
                if not entry or entry != entry.strip():
                    raise ValueError(f"{PUBLISH_PORT_LABEL} entry {entry!r} must be non-empty and unpadded")
                ports_text, protocol_separator, protocol = entry.partition("/")
                if not protocol_separator:
                    protocol = "tcp"
                published_text, port_separator, target_text = ports_text.partition(":")
                if not port_separator or not published_text.isdigit() or not target_text.isdigit():
                    raise ValueError(f"{PUBLISH_PORT_LABEL} entry {entry!r} must be a published:target port pair")
                ports.append(
                    PublishedPort.model_validate({
                        "service": service_name,
                        "published": int(published_text),
                        "target": int(target_text),
                        "protocol": protocol,
                    })
                )
    return BundleRecords(
        services=services,
        routes=routes,
        ports=ports,
        container_names=sorted(container_names),
        held_services=sorted(held_services),
        declared_ports=declared_ports,
    )


def stanza_annotations(stanza: dict[str, Yaml]) -> dict[str, str]:
    annotations: dict[str, str] = {}
    for key in ("annotations", "labels"):
        value = stanza.get(key)
        if is_mapping(value):
            annotations.update({str(name): str(item) for name, item in value.items()})
    return annotations


def _collect_declared_ports(
    declared_ports: dict[str, list[tuple[int, int, str]]], service_name: str, value: Yaml
) -> None:
    if not is_list(value):
        return
    for entry in value:
        record = _declared_port_record(entry)
        if record is not None:
            declared_ports.setdefault(service_name, []).append(record)


def _declared_port_record(entry: Yaml) -> tuple[int, int, str] | None:
    if is_mapping(entry):
        published = entry.get("published")
        target = entry.get("target")
        if isinstance(published, int) and isinstance(target, int):
            protocol = entry.get("protocol", "tcp")
            return (published, target, str(protocol))
        if len(entry) == 1:
            # Treat the short syntax as a single-pair mapping (6178:6178 parses as {6178: 6178})
            [(short_published, short_target)] = entry.items()
            if isinstance(short_target, int) and str(short_published).isdigit():
                return (int(short_published), short_target, "tcp")
        return None
    if isinstance(entry, str):
        ports, _separator, protocol = entry.partition("/")
        published, port_separator, target = ports.partition(":")
        if port_separator and published.isdigit() and target.isdigit():
            return (int(published), int(target), protocol if protocol else "tcp")
    return None


def _canonical(value: Yaml) -> Yaml:
    # Return the value with every nested dict's keys sorted
    if is_mapping(value):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if is_list(value):
        return [_canonical(item) for item in value]
    return value
