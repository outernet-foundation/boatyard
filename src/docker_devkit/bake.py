from __future__ import annotations

import difflib
import hashlib
import json
import os
import platform
import re
import shutil
from itertools import starmap
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from typing import Any, Literal, Self, TypedDict, get_args

import typer
import yaml
from bashrun.bash import bash, bash_check, bash_output
from build_artifact_registry.builds import push_build
from build_artifact_registry.setup_oras import install_oras
from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from docker_devkit.config import (
    CHART_REFERENCE_PREFIX,
    DEFAULT_MIRROR_PREFIX,
    DockerDevkitConfig,
    LOCK_PATH,
    chart_lock_key,
    load_config,
    mirrored_ref,
    optional_lock_entries,
)
from docker_devkit.daemon_gc import check_gc_limits
from docker_devkit.image_refs import ImageReference, resolve_remote_digest
from docker_devkit.service_builds import FAN_ARG, GpuFamily, compute_service_shas, service_sha_env


METADATA_PATH = Path("metadata.json")

DIGEST_PROJECT = "images-digests"
DIGEST_PLATFORM = "all"
DIGEST_FILE_NAME = "images-digests.json"

DIGEST_TAIL_PATTERN = re.compile(r"@sha256:[a-f0-9]{64}$")

type BakeValue = str | list[str] | dict[str, str]

# A flat Literal: typer cannot annotate unions and pyright cannot type a star-unpacked
# subscript, so the family membership pins by test instead of by derivation.
Gpu = Literal["auto", "none", "cuda", "rocm"]
GPU_TYPES = get_args(GpuFamily)


class DigestEntry(TypedDict):
    ref: str
    digest: str
    tags: list[str]


class BakedImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    image_name: str = Field(default="", alias="image.name")
    digest: str = Field(default="", alias="containerimage.digest")
    ref: str = ""
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _extract_references(self) -> Self:
        for reference in self.image_name.split(","):
            reference = reference.strip()
            if not reference:
                continue
            repository, separator, tag = reference.split("@", 1)[0].rpartition(":")
            if separator:
                self.ref = repository
                self.tags.append(tag)
        return self


class BakeMetadata(RootModel[dict[str, BakedImage]]):
    @model_validator(mode="after")
    def _require_tagged_references(self) -> Self:
        for target, image in self.root.items():
            if not image.tags:
                raise ValueError(f"target '{target}' produced no tagged image references from '{image.image_name}'")
        return self


def run_build(
    *,
    root: Path | None = None,
    config: DockerDevkitConfig | None = None,
    push: bool = False,
    targets_opt: list[str] | None = None,
    gpu: Gpu = "auto",
    upgrade: bool = False,
    lock_only: bool = False,
    gpu_only: bool = False,
    no_cache: bool = False,
    dev: bool = False,
    check: bool = False,
) -> None:
    # root/config inject for callers that already loaded them (up --build); the CLI
    # derives both from the working directory.
    root = root if root is not None else Path.cwd()
    config = config if config is not None else load_config(root)
    if config is None:
        if not lock_only:
            raise RuntimeError("No docker-devkit.yaml at the repo root; build derives its targets from it")
        print("no docker-devkit.yaml; no lock declarations to relock")
        return

    if dev and push:
        raise typer.BadParameter("--dev builds local-only images under the :dev tag; drop --push")
    if dev and lock_only:
        raise typer.BadParameter("--lock-only writes the lock without building; --dev selects a build shape")
    if check and not lock_only:
        raise typer.BadParameter("--check asserts the lock the --lock-only lane would write; pass --lock-only")

    lock_file = root / LOCK_PATH
    mirror_prefix = config.mirror.prefix if config.mirror is not None else DEFAULT_MIRROR_PREFIX

    service_shas = compute_service_shas(root, config.service_builds)
    os.environ.update(service_shas)

    lock_data = optional_lock_entries(root)

    if not config.service_builds and not lock_only:
        raise RuntimeError(
            "no Dockerfile service derives under this repo; build builds the derived services "
            "(relock declarations without building via --lock-only)"
        )

    if push and gpu == "auto":
        raise typer.BadParameter("With --push, --gpu cannot be 'auto'; specify 'cuda' or 'rocm'.")

    if gpu_only and gpu not in GPU_TYPES:
        raise typer.BadParameter("--gpu-only requires a concrete gpu (cuda or rocm), not 'auto' or 'none'.")

    # Check Docker GC limits so local GPU builds don't evict the build cache.
    if not push and not lock_only and not targets_opt:
        check_gc_limits()

    # Record one lock entry per declaration: the mirror-prefixed reference, digest resolved
    # against upstream — crane copies are byte-identical, so the digest holds under both
    # names. The declared tag rides the entry, so a tag bump breaks the prefix match and
    # re-resolves while an unchanged declaration copies its frozen digest forward.
    declarations: list[ImageReference] = list(starmap(ImageReference, config.base_images.items()))
    if config.k8s is not None:
        declarations += [
            ImageReference(chart_lock_key(name), reference.removeprefix(CHART_REFERENCE_PREFIX))
            for name, reference in config.k8s.charts.items()
        ]
    if lock_only and not declarations:
        print("no lock declarations (neither base_images nor k8s.charts); nothing to relock")
        return
    for occurrence in declarations:
        lock_base = mirrored_ref(occurrence.reference, mirror_prefix)
        if DIGEST_TAIL_PATTERN.search(occurrence.reference):
            lock_data[occurrence.name] = lock_base
            continue
        if not upgrade and occurrence.name in lock_data and lock_data[occurrence.name].startswith(lock_base + "@"):
            continue
        lock_data[occurrence.name] = f"{lock_base}@{resolve_remote_digest(occurrence.reference)}"

    # The lock owns exactly the declared names — an entry whose declaration died is an
    # orphan the up-time probe would still fail-closed over (an undeclared mirror digest
    # gating bring-up), so a relock prunes it.
    declared_names = {occurrence.name for occurrence in declarations}
    lock_data = {name: reference for name, reference in lock_data.items() if name in declared_names}

    lock_text = "\n".join(f"{key}={value}" for key, value in sorted(lock_data.items())) + "\n"

    if check:
        committed = lock_file.read_text(encoding="utf-8") if lock_file.exists() else ""
        if committed != lock_text:
            print(
                "".join(
                    difflib.unified_diff(
                        committed.splitlines(keepends=True),
                        lock_text.splitlines(keepends=True),
                        fromfile=f"committed {LOCK_PATH.as_posix()}",
                        tofile=f"computed {LOCK_PATH.as_posix()}",
                    )
                )
            )
            raise SystemExit("image lock is stale. Run 'build --lock-only' and commit the result.")
        return

    lock_file.write_text(lock_text, encoding="utf-8")

    if lock_only:
        return

    os.environ.update(lock_data)

    host = host_platform()

    target_variants = {
        target: variant for service in config.service_builds.values() for target, variant in service.target_variants
    }
    dev_targets = {
        target
        for service in config.service_builds.values()
        if service.dev_stage
        for target, _variant in service.target_variants
    }

    if dev and not dev_targets:
        raise RuntimeError(
            "no service declares a dev stage (a multi-stage Dockerfile carrying an AS dev stage); --dev builds those"
        )

    if targets_opt:
        requested = set(targets_opt)
        unknown = requested - set(target_variants)
        if unknown:
            raise typer.BadParameter(f"Unknown targets: {unknown}. Available: {sorted(target_variants)}")
        if dev:
            undeclared = sorted(requested - dev_targets)
            if undeclared:
                raise typer.BadParameter(f"--dev builds declared dev stages; no dev stage for {undeclared}")
        targets = [target for target in target_variants if target in requested]
    else:
        # Default targets are the native subset — the repo platforms are undeclared (the
        # host's arch) or include this host's arch — intersected with the gpu match over
        # the variant fan. An empty set refuses loudly: bake with no target arguments
        # builds every service, silently defeating the selection. Host-gpu detection lives
        # here, so an explicit --targets run never probes the host gpu.
        if gpu == "auto":
            gpu = detect_gpu()
        candidates = dev_targets if dev else set(target_variants)
        native = not config.platforms or host in config.platforms
        if gpu_only:
            targets = [
                target
                for target, variant in target_variants.items()
                if native and target in candidates and variant == gpu
            ]
        else:
            targets = [
                target
                for target, variant in target_variants.items()
                if native and target in candidates and variant in (None, gpu)
            ]
        if not targets:
            raise RuntimeError(
                f"No service targets this machine's arch ({host}) under gpu '{gpu}'; "
                f"name targets with --targets to cross-build. Services: {sorted(target_variants)}"
            )

    rendered = render_bake(config, push=push, host=host, dev=dev)

    # Drop stale metadata so the coverage check cannot pass against a previous run.
    METADATA_PATH.unlink(missing_ok=True)

    command_arguments: list[str] = [
        "--progress auto",
        "--provenance=false",
        "--sbom=false",
        "--push" if push else "--load",
    ]

    if no_cache:
        command_arguments.append("--no-cache")

    command_arguments.extend(targets)

    with TemporaryDirectory(prefix="bake-") as bake_dir:
        bake_file = Path(bake_dir) / "bake.yaml"
        bake_file.write_text(rendered, encoding="utf-8")
        _run_bake(bake_file, command_arguments, rendered)

    metadata_json: dict[str, Any] = (
        json.loads(METADATA_PATH.read_text(encoding="utf-8")) if METADATA_PATH.exists() else {}
    )
    baked = BakeMetadata.model_validate(metadata_json)
    if not set(targets) <= baked.root.keys():
        raise RuntimeError("Bake output does not cover the requested targets")

    # Shelve the digest manifest under the REGISTRY shelf, tagged with the checkout HEAD.
    # Push one shard per leg under sha-{sha}-{key}; parallel legs would last-writer-win a
    # shared tag, and the pull side merges a SHA's shards after every leg finishes.
    if push:
        registry = os.environ.get("REGISTRY", "")
        if not registry:
            raise SystemExit("REGISTRY not set — --push shelves the digest manifest under it")
        shelf = f"{registry}/builds"
        sha = bash_output("git rev-parse HEAD").strip()
        install_oras()
        digest_manifest: dict[str, DigestEntry] = {}
        for target in targets:
            image = baked.root[target]
            digest_manifest[target] = {"ref": image.ref, "digest": image.digest, "tags": image.tags}
        key = hashlib.sha256(",".join(sorted(targets)).encode("utf-8")).hexdigest()[:8]
        with TemporaryDirectory(prefix="digest-manifest-") as staging:
            manifest_path = Path(staging) / DIGEST_FILE_NAME
            manifest_path.write_text(json.dumps(digest_manifest, indent=2), encoding="utf-8")
            push_build(
                shelf,
                DIGEST_PROJECT,
                DIGEST_PLATFORM,
                f"sha-{sha}-{key}",
                Path(staging),
                [DIGEST_FILE_NAME],
            )


def host_platform() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "linux/amd64"
    if machine in ("aarch64", "arm64"):
        return "linux/arm64"
    raise RuntimeError(f"Unsupported host architecture: {machine}")


def detect_gpu() -> Gpu:
    # The which() pre-guard is load-bearing: bash_check execs without a shell, so a
    # missing binary would raise instead of answering False.
    if shutil.which("nvidia-smi") and bash_check("nvidia-smi"):
        return "cuda"
    if shutil.which("rocminfo") and bash_check("rocminfo"):
        return "rocm"
    raise RuntimeError("Could not detect GPU type.")


def render_bake(config: DockerDevkitConfig, *, push: bool, host: str, dev: bool = False) -> str:
    # The per-run temp bake file: bake validates its own input by failing on it, so no
    # model sits behind the render. Identical configs render byte-identical files.
    services: dict[str, dict[str, dict[str, BakeValue]]] = {}
    for service in config.service_builds.values():
        for target, variant in service.target_variants:
            args: dict[str, str] = {key: f"${{{key}:-}}" for key in config.base_images}
            if variant is not None:
                args[FAN_ARG] = variant
            args.update({sha: f"${{{sha}:?err}}" for sha in service.sha_args})
            build: dict[str, BakeValue] = {"dockerfile": service.dockerfile}
            if dev:
                build["target"] = "dev"
            if config.platforms:
                # A --load run writes a single arch, so a multi-arch declaration collapses
                # to the host; a single declared platform rides through in both modes.
                build["platforms"] = [host] if not push and len(config.platforms) > 1 else list(config.platforms)
            build["args"] = args
            if dev:
                # The dev tag is per service, not per target: whichever gpu variant a host
                # builds, the overlay references one {repository}/{service}:dev image.
                build["tags"] = [f"{config.repository}/{service.name}:dev"]
            else:
                build["tags"] = [
                    f"{config.repository}/{target}:latest",
                    f"{config.repository}/{target}:${{{service_sha_env(service.name)}}}",
                ]
            if service.contexts:
                build["additional_contexts"] = {
                    context: f"target:{context}-{variant}" if variant is not None else f"target:{context}"
                    for context in service.contexts
                }
            if push:
                cache = f"{config.repository}/build-cache"
                build["cache_to"] = [
                    f"type=registry,ref={cache}:{target},mode=max,image-manifest=true,oci-mediatypes=true"
                ]
                build["cache_from"] = [f"type=registry,ref={cache}:{target}"]
            services[target] = {"build": build}
    return yaml.safe_dump({"services": services}, sort_keys=False)


def _run_bake(bake_file: Path, command_arguments: list[str], rendered: str) -> None:
    # The temp bake file vanishes with its directory, so a failed bake re-raises with the
    # rendered content attached — the failure stays inspectable after the run.
    try:
        bash(
            " ".join(["docker buildx bake", f"-f {bake_file}", f"--metadata-file {METADATA_PATH}", *command_arguments])
        )
    except CalledProcessError as error:
        raise RuntimeError(
            f"docker buildx bake failed (exit {error.returncode}); rendered bake file:\n{rendered}"
        ) from error
