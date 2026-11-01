from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from typing import Any, Self

import typer
from bashrun.bash import bash
from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from docker_devkit.config import DockerDevkitConfig, load_config
from docker_devkit.daemon_gc import check_gc_limits
from docker_devkit.lock import env_entries, write_lock
from docker_devkit.config import ServiceBuild


METADATA_PATH = Path("metadata.json")


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
    push: str | None = None,
    targets_opt: list[str] | None = None,
    no_cache: bool = False,
    dev: bool = False,
) -> None:
    # root/config inject for callers that already loaded them (up --build); the CLI
    # derives both from the working directory. push is the destination axis: None builds
    # local, "" (--push alone) pushes to the config's repository, an address (--push
    # <addr>) pushes to that registry.
    root = root if root is not None else Path.cwd()
    config = config if config is not None else load_config(root)
    if config is None:
        raise RuntimeError("No docker-devkit.yaml at the repo root; build derives its targets from it")

    pushing = push is not None
    if dev and pushing:
        raise typer.BadParameter("--dev builds local-only images under the :dev tag; drop --push")

    destination = push if push else None
    if destination is not None:
        host = destination.partition("/")[0]
        if "://" in destination or destination.endswith("/") or ("." not in host and ":" not in host):
            raise typer.BadParameter(
                f"--push destination {destination!r} is a registry address (localhost:5000, ghcr.io/org) "
                "with no scheme and no trailing slash"
            )
    elif pushing and config.repository is None:
        raise RuntimeError("--push with no address builds for the config's repository; this repo declares none")

    service_shas = config.service_shas()
    os.environ.update(service_shas)

    if not config.service_builds:
        raise RuntimeError(
            "no Dockerfile service derives under this repo; build builds the derived services "
            "(relock declarations without building via the lock verb)"
        )

    # Check Docker GC limits so local builds don't evict the build cache.
    if not pushing and not targets_opt:
        check_gc_limits()

    # The incremental lock write rides every building run (the uv sync shape): anyone who
    # builds also locks, and bake reads the freshly written entries as environment.
    written = write_lock(config)
    os.environ.update(env_entries(written))

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
        targets: list[str] = []
        for address in targets_opt:
            target = _resolve_target(address, config.service_builds, target_variants)
            if target not in targets:
                targets.append(target)
        if dev:
            undeclared = sorted(set(targets) - dev_targets)
            if undeclared:
                raise typer.BadParameter(f"--dev builds declared dev stages; no dev stage for {undeclared}")
    else:
        # Default targets are every service's default cell (the bare-name selection: the
        # first declared value) restricted to this machine's arch — the repo platforms are
        # undeclared (the host's arch) or include it. An empty set refuses loudly: bake
        # with no target arguments builds every service, silently defeating the selection.
        defaults = {service.target_variants[0][0] for service in config.service_builds.values()}
        selected = defaults & dev_targets if dev else defaults
        targets = sorted(selected) if not config.platforms or host in config.platforms else []
        if not targets:
            raise RuntimeError(
                f"No service targets this machine's arch ({host}); "
                f"name targets with --targets to cross-build. Services: {_address_vocabulary(config.service_builds)}"
            )

    rendered = config.render_bake(push=pushing, host=host, destination=destination, dev=dev)

    # Drop stale metadata so the coverage check cannot pass against a previous run; the
    # file left in the working directory after the run is the shelf handoff the toolkit's
    # build workflow reads.
    METADATA_PATH.unlink(missing_ok=True)

    command_arguments: list[str] = [
        "--progress auto",
        "--provenance=false",
        "--sbom=false",
        "--push" if pushing else "--load",
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


def host_platform() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "linux/amd64"
    if machine in ("aarch64", "arm64"):
        return "linux/arm64"
    raise RuntimeError(f"Unsupported host architecture: {machine}")


def _resolve_target(
    address: str,
    services: dict[str, ServiceBuild],
    target_variants: dict[str, str | None],
) -> str:
    # A bare name selects the default cell; name/cell addresses one declared cell; a
    # suffixed target name (what the matrix emits) addresses it directly.
    if "/" in address:
        service_name, _, cell = address.partition("/")
        service = services.get(service_name)
        if service is None:
            raise typer.BadParameter(f"Unknown target {address!r}. Available: {_address_vocabulary(services)}")
        if not service.variants:
            raise typer.BadParameter(f"{service_name} declares no variant cells; build it as {service_name!r}")
        if cell not in service.variants:
            raise typer.BadParameter(
                f"Unknown cell {cell!r} for {service_name}; it declares the cells {service.variants}"
            )
        return f"{service_name}-{cell}"
    if address in services:
        return services[address].target_variants[0][0]
    if address in target_variants:
        return address
    raise typer.BadParameter(f"Unknown target {address!r}. Available: {_address_vocabulary(services)}")


def _address_vocabulary(services: dict[str, ServiceBuild]) -> list[str]:
    addresses: list[str] = []
    for name in sorted(services):
        addresses.append(name)
        addresses.extend(f"{name}/{variant}" for variant in services[name].variants)
    return addresses


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
