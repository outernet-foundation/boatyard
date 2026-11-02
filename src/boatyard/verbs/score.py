from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, NamedTuple

import typer
import yaml
from bashrun.bash import bash, bash_check, bash_output

from boatyard import dockerfiles
from boatyard.config import COMPOSE_HOLE, Config, Target, load_yaml_file
from boatyard.config_lock import LOCK_PATH, parse_config_lock
from boatyard.score import (
    SECRETS_FILE,
    SECRETS_OUTPUT,
    SecretsDeclaration,
    collect_secret_keys,
    split_documents,
    write_generated_file,
)
from boatyard.score_compose import generate_compose
from boatyard.score_k8s import generate_k8s

app = typer.Typer()


class ToolRequirement(NamedTuple):
    binary: str
    version: str
    probe: str
    archive: str
    extract: str


@app.command()
def generate_score(
    target: Annotated[Target, typer.Option(help="Which Score artifact to generate.")] = "both",
    local: Annotated[
        bool,
        typer.Option(
            "--local",
            help="Generate for a local cluster: local storage class, written to the configured local output path.",
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Regenerate and exit 1 when the committed artifacts are stale."),
    ] = False,
) -> None:
    if check and local:
        raise typer.BadParameter("--check and --local are mutually exclusive")
    root = Path.cwd()
    config = Config.load(root)
    if config is None or not (config.compose_bundles() or config.k8s):
        if not check:
            raise ValueError("boatyard.yml declares no local service entries and no k8s section")
        print("no local service entries and no k8s section; nothing to check")
        return

    compose_rendering = target in ("both", "compose")
    k8s_rendering = target in ("both", "k8s") and config.k8s is not None
    if config.k8s is None and target == "k8s":
        raise typer.BadParameter("--target k8s: this config declares no k8s section (compose-only)")
    if config.k8s is None and local:
        raise typer.BadParameter("--local requires a k8s section; this config declares none")

    ensure_tools(
        [
            ToolRequirement(
                binary=binary,
                version=version,
                probe=f"{binary} {version}",
                archive=(
                    f"https://github.com/score-spec/{binary}/releases/download/{version}/"
                    f"{binary}_{version}_linux_amd64.tar.gz"
                ),
                extract=binary,
            )
            for binary, version in (
                ("score-compose", config.score_compose_version),
                ("score-k8s", config.score_k8s_version),
            )
        ]
        + [
            ToolRequirement(
                binary="helm",
                version=config.helm_version,
                probe=f'"v{config.helm_version}"',
                archive=f"https://get.helm.sh/helm-v{config.helm_version}-linux-amd64.tar.gz",
                extract="--strip-components=1 linux-amd64/helm",
            )
        ]
    )

    # Require the build keys, derive the tree tags, and load the lock.
    if not (config.repository or config.base_images):
        raise ValueError("repository or base_images is required in boatyard.yml")
    service_shas = dockerfiles.compute_service_shas(root, config.service_builds)
    lock_path = root / LOCK_PATH
    if not lock_path.is_file():
        raise ValueError(f"{lock_path.as_posix()}: the image lock is required")
    lock = parse_config_lock(lock_path)
    if not lock.images and not lock.charts:
        raise ValueError(f"{lock_path.as_posix()}: the image lock carries no entries")
    outputs: list[Path] = []
    k8s_output: Path | None = None
    if compose_rendering:
        outputs.extend(generate_compose(config, lock, service_shas))

    if k8s_rendering:
        k8s_output = generate_k8s(config, lock, service_shas, local)
        outputs.append(k8s_output)

    if (root / SECRETS_FILE).is_file():
        # Validate the consumed keys and write the mint list.
        declaration_path = root / SECRETS_FILE
        k8s_secret = f"{config.project_name}-secrets"

        declaration = SecretsDeclaration.model_validate(load_yaml_file(declaration_path))

        if compose_rendering:
            consumed: set[str] = set()
            for bundle in config.compose_bundles():
                for match in COMPOSE_HOLE.finditer(bundle.output.read_text(encoding="utf-8")):
                    if len(match.group(1)) % 2 == 1:
                        consumed.add(match.group(2))
            declaration.validate_consumed(consumed, "compose", SECRETS_FILE.as_posix())

        if k8s_rendering and k8s_output is not None:
            consumed = set()
            for block in split_documents(k8s_output.read_text(encoding="utf-8")):
                collect_secret_keys(yaml.safe_load(block), k8s_secret, consumed)
            declaration.validate_consumed(consumed, "k8s", SECRETS_FILE.as_posix())

        output = root / SECRETS_OUTPUT
        output.parent.mkdir(parents=True, exist_ok=True)
        write_generated_file(
            output,
            "# Generated by generate-score from the secrets declaration\n"
            + "".join(f"{name}\n" for name in sorted(name for name, key in declaration.root.items() if key.mint)),
        )
        outputs.append(output)

    if check:
        pathspec = " ".join(output.as_posix() for output in outputs)
        if bash_output(f"git status --porcelain -- {pathspec}").strip():
            bash(f"git diff -- {pathspec}")
            raise SystemExit(
                "score artifacts are stale. Run 'generate-score' and commit the result as 'Run generate-score'."
            )


def ensure_tools(requirements: list[ToolRequirement]) -> None:
    for requirement in requirements:
        if bash_check(f"{requirement.binary} version") and requirement.probe in bash_output(
            f"{requirement.binary} version"
        ):
            continue
        print(f"Install {requirement.binary} {requirement.version}")
        sudo = "sudo " if os.geteuid() != 0 else ""
        with TemporaryDirectory() as directory:
            archive = Path(directory) / Path(requirement.archive).name
            bash(f"curl -fsSL {requirement.archive} -o {archive.as_posix()}")
            bash(f"{sudo}tar -xzf {archive.as_posix()} -C /usr/local/bin {requirement.extract}")
