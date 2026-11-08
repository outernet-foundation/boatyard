from __future__ import annotations

import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

import typer
from bashrun.bash import bash, bash_check, bash_output

from boatyard.models.config_file import ConfigFile, Target
from boatyard.models.lock_file import LOCK_PATH, LockFile
from boatyard.score import SECRETS_OUTPUT, write_file
from boatyard.score_compose import generate_compose
from boatyard.score_k8s import generate_k8s


class ToolRequirement(NamedTuple):
    binary: str
    version: str
    probe: str
    archive: str
    extract: str


def generate_artifacts(
    config: ConfigFile,
    *,
    target: Target | None = None,
    local: bool = False,
) -> list[Path]:
    local_entries = config.local_service_entries()

    if target is None:
        target = "both" if (local_entries and config.k8s is not None) else ("compose" if local_entries else "k8s")

    if config.k8s is None and target in ("k8s", "both"):
        message = (
            "--target k8s: this config declares no k8s section (compose-only)"
            if target == "k8s"
            else "--target both: this config declares no k8s section; pass --target compose"
        )
        raise typer.BadParameter(message)
    if not local_entries and target in ("compose", "both"):
        message = (
            "--target compose: this config declares no local service entries (k8s-only)"
            if target == "compose"
            else "--target both: this config declares no local service entries; pass --target k8s"
        )
        raise typer.BadParameter(message)
    if config.k8s is None and local:
        raise typer.BadParameter("--local requires a k8s section; this config declares none")

    # Require the build keys and load the lock
    if not (config.repository or config.base_images):
        raise ValueError("repository or base_images is required in boatyard.yml")
    root = config.root
    lock_path = root / LOCK_PATH
    if not lock_path.is_file():
        raise ValueError(f"{lock_path.as_posix()}: the image lock is required")
    lock = LockFile.load(lock_path, config)
    if not lock.images and not lock.charts:
        raise ValueError(f"{lock_path.as_posix()}: the image lock carries no entries")

    # Require the secrets section and treat an empty mapping as the stated empty declaration
    secrets = config.secrets
    if secrets is None:
        raise ValueError("boatyard.yml: the secrets section is required; secrets: {} states zero secrets")

    requirements: list[ToolRequirement] = []
    if target in ("both", "compose"):
        requirements.append(
            ToolRequirement(
                binary="score-compose",
                version=config.score_compose_version,
                probe=f"score-compose {config.score_compose_version}",
                archive=(
                    f"https://github.com/score-spec/score-compose/releases/download/{config.score_compose_version}/"
                    f"score-compose_{config.score_compose_version}_linux_amd64.tar.gz"
                ),
                extract="score-compose",
            )
        )
    if target in ("both", "k8s"):
        requirements += [
            ToolRequirement(
                binary="score-k8s",
                version=config.score_k8s_version,
                probe=f"score-k8s {config.score_k8s_version}",
                archive=(
                    f"https://github.com/score-spec/score-k8s/releases/download/{config.score_k8s_version}/"
                    f"score-k8s_{config.score_k8s_version}_linux_amd64.tar.gz"
                ),
                extract="score-k8s",
            ),
            ToolRequirement(
                binary="helm",
                version=config.helm_version,
                probe=f'"v{config.helm_version}"',
                archive=f"https://get.helm.sh/helm-v{config.helm_version}-linux-amd64.tar.gz",
                extract="--strip-components=1 linux-amd64/helm",
            ),
        ]
    for requirement in requirements:
        if (
            shutil.which(requirement.binary) is not None
            and bash_check(f"{requirement.binary} version")
            and requirement.probe in bash_output(f"{requirement.binary} version")
        ):
            continue
        print(f"Install {requirement.binary} {requirement.version}")
        sudo = "sudo " if os.geteuid() != 0 else ""
        with TemporaryDirectory() as directory:
            archive = Path(directory) / Path(requirement.archive).name
            bash(f"curl -fsSL {requirement.archive} -o {archive.as_posix()}")
            bash(f"{sudo}tar -xzf {archive.as_posix()} -C /usr/local/bin {requirement.extract}")

    outputs: list[Path] = []
    if target in ("both", "compose"):
        outputs.extend(generate_compose(config, lock))

    if target in ("both", "k8s") and config.k8s is not None:
        outputs.append(generate_k8s(config, config.k8s, lock, local, secrets))

    secrets_output = root / SECRETS_OUTPUT
    secrets_output.parent.mkdir(parents=True, exist_ok=True)
    write_file(
        secrets_output,
        "# Generated by generate-score from the secrets declaration\n"
        + "".join(f"{name}\n" for name, key in sorted(secrets.root.items()) if key.mint),
    )
    outputs.append(secrets_output)

    return outputs
