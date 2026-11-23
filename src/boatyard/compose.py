from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from bashrun.bash import bash, bash_check, bash_output

COMPOSE_VERSION = "v5.6.1"

COMPOSE_CACHE_DIRECTORY = Path.home() / ".cache" / "boatyard" / "compose"

_installed = False


def install_compose() -> None:
    global _installed
    if _installed:
        return
    directory = compose_binary().parent.as_posix()
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ['PATH']}"
    _installed = True


def compose_binary() -> Path:
    if sys.platform == "win32":
        return _compose_binary_windows()
    return _compose_binary_posix()


def _compose_binary_posix() -> Path:
    architecture = {"x86_64": "x86_64", "aarch64": "aarch64"}[platform.machine()]
    binary = COMPOSE_CACHE_DIRECTORY / COMPOSE_VERSION / architecture / "docker-compose"
    release = (
        f"https://github.com/docker/compose/releases/download/{COMPOSE_VERSION}/docker-compose-linux-{architecture}"
    )
    return _resolved_binary(binary, release)


def _compose_binary_windows() -> Path:
    binary = COMPOSE_CACHE_DIRECTORY / COMPOSE_VERSION / "x86_64" / "docker-compose.exe"
    release = f"https://github.com/docker/compose/releases/download/{COMPOSE_VERSION}/docker-compose-Windows-x86_64.exe"
    return _resolved_binary(binary, release)


def _resolved_binary(binary: Path, release: str) -> Path:
    # The exact compose binary every verb invokes, cached under ~/.cache/boatyard so one
    # download serves every run. The download lands on a partial name and renames into
    # place only after its version output checks out — a killed or corrupt download
    # never poisons the cache.
    if binary.is_file() and bash_check(f"{binary.as_posix()} version"):
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    partial = binary.with_name(f"{binary.name}.partial")
    bash(f"curl -fsSL {release} -o {partial.as_posix()}")
    partial.chmod(0o755)
    if COMPOSE_VERSION not in bash_output(f"{partial.as_posix()} version"):
        raise ValueError(f"{partial.as_posix()}: the downloaded binary does not report version {COMPOSE_VERSION}")
    partial.replace(binary)
    return binary
