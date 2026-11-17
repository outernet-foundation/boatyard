from __future__ import annotations

import os
import shutil
import sys
import tarfile
from pathlib import Path

from bashrun.bash import bash

CRANE_VERSION = "v0.22.1"

WINDOWS_CRANE_DIRECTORY = Path.home() / ".cache" / "boatyard" / "crane"

_installed = False


def install_crane() -> None:
    global _installed
    if _installed:
        return
    if sys.platform == "win32":
        _install_crane_windows()
    else:
        _install_crane_posix()
    _installed = True


def _install_crane_posix() -> None:
    print(f"Install crane {CRANE_VERSION}")
    sudo = "sudo " if os.geteuid() != 0 else ""
    archive = "go-containerregistry_Linux_x86_64.tar.gz"
    bash(f"curl -fsSLO https://github.com/google/go-containerregistry/releases/download/{CRANE_VERSION}/{archive}")
    bash(f"{sudo}tar -xzf {archive} -C /usr/local/bin crane")
    Path(archive).unlink(missing_ok=True)


def _install_crane_windows() -> None:
    if shutil.which("crane") is not None:
        return
    print(f"Install crane {CRANE_VERSION}")
    WINDOWS_CRANE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    archive_name = "go-containerregistry_Windows_x86_64.tar.gz"
    release = f"https://github.com/google/go-containerregistry/releases/download/{CRANE_VERSION}/{archive_name}"
    bash(f"curl -fsSL {release} -o {archive_name}", cwd=WINDOWS_CRANE_DIRECTORY)
    archive_path = WINDOWS_CRANE_DIRECTORY / archive_name
    with tarfile.open(archive_path) as archive:
        archive.extract("crane.exe", WINDOWS_CRANE_DIRECTORY, filter="data")
    archive_path.unlink()
    os.environ["PATH"] = f"{WINDOWS_CRANE_DIRECTORY}{os.pathsep}{os.environ['PATH']}"
