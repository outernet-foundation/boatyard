from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from typing import Any, Self

import typer
import yaml
from bashrun.bash import bash, bash_check, bash_output
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from docker_devkit.config import DockerDevkitConfig, ServiceBuild, load_config, service_sha_env
from docker_devkit.lock import env_entries, write_lock


METADATA_PATH = Path("metadata.json")

MIN_KEEP_STORAGE_GB = 60

type BakeValue = str | list[str] | dict[str, str]


class BakeMetadata(RootModel[dict[str, "BakedImage"]]):
    @model_validator(mode="after")
    def _require_tagged_references(self) -> Self:
        for target, image in self.root.items():
            if not image.tags:
                raise ValueError(f"target '{target}' must produce tagged image references")
        return self


class BakedImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    image_name: str = Field(default="", alias="image.name")
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _extract_references(self) -> Self:
        for reference in self.image_name.split(","):
            reference = reference.strip()
            if not reference:
                continue
            _repository, separator, tag = reference.split("@", 1)[0].rpartition(":")
            if separator:
                self.tags.append(tag)
        return self


class _DaemonGc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gigabytes: float | None = Field(default=None, alias="defaultKeepStorage")

    @field_validator("gigabytes", mode="before")
    @classmethod
    def _measure(cls, value: object) -> object:
        if value is None:
            return None
        match = re.match(r"^(\d+(?:\.\d+)?)\s*([TGMK]i?B)?$", str(value), re.IGNORECASE)
        if match is None:
            raise ValueError(f"defaultKeepStorage value {value!r} must be a number with an optional unit (60GB, 1TiB)")
        unit = (match.group(2) or "B")[0].upper()
        multipliers = {"T": 1024, "G": 1, "M": 1 / 1024, "K": 1 / 1024**2, "B": 1 / 1024**3}
        return float(match.group(1)) * multipliers[unit]


class _DaemonBuilder(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gc: _DaemonGc = Field(default_factory=_DaemonGc)


class _DaemonConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    builder: _DaemonBuilder = Field(default_factory=_DaemonBuilder)


def run_build(
    *,
    root: Path | None = None,
    config: DockerDevkitConfig | None = None,
    push: str | None = None,
    targets_opt: list[str] | None = None,
    no_cache: bool = False,
    dev: bool = False,
) -> None:
    root = root if root is not None else Path.cwd()
    config = config if config is not None else load_config(root)
    if config is None:
        raise RuntimeError("No docker-devkit.yaml at the repo root")

    pushing = push is not None
    if dev and pushing:
        raise typer.BadParameter("--dev builds local-only images; --push is not allowed")

    destination = push if push else None
    if destination is not None:
        registry_host = destination.partition("/")[0]
        if "://" in destination or destination.endswith("/") or ("." not in registry_host and ":" not in registry_host):
            raise typer.BadParameter(
                f"--push destination {destination!r} must be a registry address (localhost:5000, ghcr.io/org) "
                "with no scheme and no trailing slash"
            )
    elif pushing and config.repository is None:
        raise RuntimeError("--push is declared with no address; repository is required")

    service_shas = config.service_shas()
    os.environ.update(service_shas)

    if not config.service_builds:
        raise RuntimeError("no Dockerfile service derives under this repo; the lock verb owns relocking declarations")

    # Check Docker GC limits.
    if not pushing and not targets_opt:
        check_gc_limits()

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
        raise RuntimeError("no service declares a dev stage (a multi-stage Dockerfile carrying an AS dev stage)")

    if targets_opt:
        targets: list[str] = []
        for address in targets_opt:
            # Resolve each address to a target.
            if "/" in address:
                service_name, _, cell = address.partition("/")
                service = config.service_builds.get(service_name)
                if service is None:
                    raise typer.BadParameter(
                        f"Unknown target {address!r}. Available: {_address_vocabulary(config.service_builds)}"
                    )
                if not service.variants:
                    raise typer.BadParameter(f"{service_name} declares no variant cells")
                if cell not in service.variants:
                    raise typer.BadParameter(
                        f"Unknown cell {cell!r} for {service_name}; it declares the cells {service.variants}"
                    )
                target = f"{service_name}-{cell}"
            elif address in config.service_builds:
                target = config.service_builds[address].target_variants[0][0]
            elif address in target_variants:
                target = address
            else:
                raise typer.BadParameter(
                    f"Unknown target {address!r}. Available: {_address_vocabulary(config.service_builds)}"
                )
            if target not in targets:
                targets.append(target)
        if dev:
            undeclared = sorted(set(targets) - dev_targets)
            if undeclared:
                raise typer.BadParameter(f"--dev: no dev stage for {undeclared}")
    else:
        # Select the default targets for this machine's arch.
        defaults = {service.target_variants[0][0] for service in config.service_builds.values()}
        targets = (
            sorted(defaults & dev_targets if dev else defaults)
            if not config.platforms or host in config.platforms
            else []
        )
        if not targets:
            raise RuntimeError(
                f"No service targets this machine's arch ({host}); "
                f"other arches require --targets. Services: {_address_vocabulary(config.service_builds)}"
            )

    # Write the incremental lock and load its entries into the environment.
    written = write_lock(config)
    os.environ.update(env_entries(written))

    # Render the bake services for every target.
    namespace = destination if destination is not None else config.repository
    services: dict[str, dict[str, dict[str, BakeValue]]] = {}
    for service in config.service_builds.values():
        for target, variant in service.target_variants:
            args: dict[str, str] = {key: f"${{{key}:-}}" for key in config.base_images}
            if variant is not None:
                assert service.axis is not None
                args[service.axis] = variant
            args.update({sha: f"${{{sha}:?err}}" for sha in service.sha_args})
            build: dict[str, BakeValue] = {"dockerfile": service.dockerfile}
            if dev:
                build["target"] = "dev"
            if config.platforms:
                # Collapse multi-arch declarations to the host arch in load mode.
                build["platforms"] = [host] if not pushing and len(config.platforms) > 1 else list(config.platforms)
            build["args"] = args
            if dev:
                # Tag dev builds per service, not per target.
                build["tags"] = [f"{namespace}/{service.name}:dev"]
            else:
                build["tags"] = [
                    f"{namespace}/{target}:latest",
                    f"{namespace}/{target}:${{{service_sha_env(service.name)}}}",
                ]
            if service.contexts:
                build["additional_contexts"] = {
                    context: f"target:{context}-{variant}" if variant is not None else f"target:{context}"
                    for context in service.contexts
                }
            if pushing:
                cache = f"{namespace}/build-cache"
                build["cache_to"] = [
                    f"type=registry,ref={cache}:{target},mode=max,image-manifest=true,oci-mediatypes=true"
                ]
                build["cache_from"] = [f"type=registry,ref={cache}:{target}"]
            services[target] = {"build": build}
    rendered = yaml.safe_dump({"services": services}, sort_keys=False)

    # Drop stale metadata from a previous run.
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
        # Re-raise a failed bake with the rendered content attached.
        try:
            bash(
                " ".join([
                    "docker buildx bake",
                    f"-f {bake_file}",
                    f"--metadata-file {METADATA_PATH}",
                    *command_arguments,
                ])
            )
        except CalledProcessError as error:
            raise RuntimeError(
                f"docker buildx bake failed (exit {error.returncode}); rendered bake file:\n{rendered}"
            ) from error

    metadata_json: dict[str, Any] = (
        json.loads(METADATA_PATH.read_text(encoding="utf-8")) if METADATA_PATH.exists() else {}
    )
    baked = BakeMetadata.model_validate(metadata_json)
    if not set(targets) <= baked.root.keys():
        raise RuntimeError("Bake output does not cover the requested targets")


def check_gc_limits(candidates: list[Path] | None = None) -> None:
    # Probe the system file, the user file, then the Windows home under WSL.
    if candidates is None:
        candidates = [Path("/etc/docker/daemon.json"), Path(os.path.expanduser("~/.docker/daemon.json"))]
        wslpath = 'wslpath $(cmd.exe /c "echo %UserProfile%" 2>/dev/null)'
        if os.getenv("WSL_DISTRO_NAME") is not None and bash_check(wslpath):
            win_home = bash_output(wslpath).strip()
            candidates.append(Path(win_home) / ".docker" / "daemon.json")

    config = next((path for path in candidates if os.access(path, os.R_OK)), None)
    if config is None:
        print(f"    [WARN] No readable daemon.json found; defaultKeepStorage must be >= {MIN_KEEP_STORAGE_GB}GB.")
        return

    daemon = _DaemonConfig.model_validate(json.loads(config.read_text(encoding="utf-8")))
    gigabytes = daemon.builder.gc.gigabytes
    if gigabytes is None:
        sample = json.dumps({"builder": {"gc": {"defaultKeepStorage": f"{MIN_KEEP_STORAGE_GB}GB"}}}, indent=2)
        raise RuntimeError(
            f"builder.gc.defaultKeepStorage missing in {config}; must be >= {MIN_KEEP_STORAGE_GB}GB:\n\n{sample}"
        )
    if gigabytes < MIN_KEEP_STORAGE_GB:
        raise RuntimeError(
            f"Docker GC limit {gigabytes:.1f}GB in {config} is below the required {MIN_KEEP_STORAGE_GB}GB"
        )

    print(f"    [OK] Docker GC Limit verified: {gigabytes:.1f}GB")


def host_platform() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "linux/amd64"
    if machine in ("aarch64", "arm64"):
        return "linux/arm64"
    raise RuntimeError(f"Unsupported host architecture: {machine}")


def _address_vocabulary(services: dict[str, ServiceBuild]) -> list[str]:
    addresses: list[str] = []
    for name in sorted(services):
        addresses.append(name)
        addresses.extend(f"{name}/{variant}" for variant in services[name].variants)
    return addresses
