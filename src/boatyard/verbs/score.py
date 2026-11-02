from __future__ import annotations

import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, NamedTuple

import typer
import yaml
from bashrun.bash import bash, bash_output

from boatyard import dockerfiles
from boatyard.compose import merged_environment
from boatyard.config import COMPOSE_HOLE, Config, Target, validate
from boatyard.config_lock import LOCK_PATH, parse_config_lock
from boatyard.lock import compose_env_entries
from boatyard.score import (
    SECRETS_FILE,
    SECRETS_OUTPUT,
    SecretsDeclaration,
    collect_secret_keys,
    load_yaml_file,
    split_documents,
    write_generated_file,
)
from boatyard.score_compose import generate_compose
from boatyard.score_k8s import CLOUD_STORAGE_CLASS, LOCAL_STORAGE_CLASS, generate_k8s


class ToolRequirement(NamedTuple):
    binary: str
    version: str
    release: str
    archive: str
    extract: str


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
        raise RuntimeError("--check and --local are mutually exclusive")
    root = Path.cwd()
    config = Config.load(root)
    bundles = config.compose_bundles() if config is not None else []
    if config is None or (not bundles and config.k8s is None):
        if not check:
            raise RuntimeError("boatyard.yml declares no local service entries and no k8s section")
        print("no local service entries and no k8s section; nothing to check")
        return
    k8s_rendering = config.k8s is not None

    if not k8s_rendering and target == "k8s":
        raise typer.BadParameter("--target k8s: this config declares no k8s section (compose-only)")
    if not k8s_rendering and local:
        raise typer.BadParameter("--local requires a k8s section; this config declares none")

    # The pinned tools this run needs at exact versions.
    renderings = rendering_targets(config, target)
    score_tools: list[tuple[str, str]] = []
    if "compose" in renderings:
        score_tools.append(("score-compose", config.score_compose_version))
    if "k8s" in renderings:
        score_tools.append(("score-k8s", config.score_k8s_version))
    requirements = [
        ToolRequirement(
            binary=binary,
            version=version,
            release=f"https://github.com/score-spec/{binary}/releases/tag/{version}",
            archive=f"https://github.com/score-spec/{binary}/releases/download/{version}/{binary}_{version}_linux_amd64.tar.gz",
            extract=binary,
        )
        for binary, version in score_tools
    ]
    if "k8s" in renderings and config.k8s is not None and config.k8s.charts:
        version = config.helm_version
        requirements.append(
            ToolRequirement(
                binary="helm",
                version=version,
                release=f"https://github.com/helm/helm/releases/tag/v{version}",
                archive=f"https://get.helm.sh/helm-v{version}-linux-amd64.tar.gz",
                extract="--strip-components=1 linux-amd64/helm",
            )
        )

    if check:
        # Fetch the pinned tools into a per-run temp bin.
        with TemporaryDirectory(prefix="score-tools-") as temporary_directory:
            bin_dir = Path(temporary_directory) / "bin"
            bin_dir.mkdir()
            for requirement in requirements:
                archive = bin_dir / Path(requirement.archive).name
                bash(f"curl -fsSL {requirement.archive} -o {archive.as_posix()}")
                bash(f"tar -xzf {archive.as_posix()} -C {bin_dir} {requirement.extract}")
                archive.unlink()
            tool_environment = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
            outputs = generate_artifacts(config, target, False, tool_environment)
            pathspec = " ".join(output.as_posix() for output in outputs)
            if bash_output(f"git status --porcelain -- {pathspec}").strip():
                bash(f"git diff -- {pathspec}")
                raise SystemExit(
                    "score artifacts are stale. Run 'generate-score' and commit the result as 'Run generate-score'."
                )
        return

    missing = [item for item in requirements if shutil.which(item.binary) is None]
    if missing:
        lines = [f"  {item.binary} {item.version}: {item.release}" for item in missing]
        raise RuntimeError("generate-score requires these on PATH at these exact versions:\n" + "\n".join(lines))
    generate_artifacts(config, target, local)


def generate_artifacts(
    config: Config, target: Target, local: bool, tool_environment: dict[str, str] | None = None
) -> list[Path]:
    # Require the build keys, derive the tree tags, and load the lock.
    if not (config.repository or config.base_images):
        raise ValueError("repository or base_images is required in boatyard.yml")
    root = config.root
    service_shas = dockerfiles.compute_service_shas(root, config.service_builds)
    lock_path = root / LOCK_PATH
    if not lock_path.is_file():
        raise ValueError(f"{lock_path.as_posix()}: the image lock is required")
    lock = parse_config_lock(lock_path)
    if not lock.images and not lock.charts:
        raise ValueError(f"{lock_path.as_posix()}: the image lock carries no entries")
    k8s_output = root / (config.k8s_local_output if local else config.k8s_output)

    outputs: list[Path] = []
    renderings = rendering_targets(config, target)

    if "compose" in renderings:
        generate_compose(
            root, config, compose_env_entries(lock, config, merged_environment(root)), service_shas, tool_environment
        )
        outputs.extend(bundle.output for bundle in config.compose_bundles())

    if "k8s" in renderings and config.k8s is not None:
        storage_class = LOCAL_STORAGE_CLASS if local else CLOUD_STORAGE_CLASS
        generate_k8s(root, config, config.k8s, k8s_output, lock, service_shas, storage_class, tool_environment)
        outputs.append(k8s_output)

    if (root / SECRETS_FILE).is_file():
        # Validate the consumed keys and write the mint list.
        declaration_path = root / SECRETS_FILE
        k8s_secret = f"{config.project_name}-secrets"

        declaration = validate(SecretsDeclaration, load_yaml_file(declaration_path), declaration_path.as_posix())

        if target in ("both", "compose"):
            consumed: set[str] = set()
            for bundle in config.compose_bundles():
                for match in COMPOSE_HOLE.finditer(bundle.output.read_text(encoding="utf-8")):
                    if len(match.group(1)) % 2 == 1:
                        consumed.add(match.group(2))
            declaration.validate_consumed(consumed, "compose", SECRETS_FILE.as_posix())

        if target in ("both", "k8s") and config.k8s is not None:
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

    return outputs


def rendering_targets(config: Config, target: Target) -> list[Literal["compose", "k8s"]]:
    # The one fan-out decider: which renderings a run produces.
    renderings: list[Literal["compose", "k8s"]] = []
    if target in ("both", "compose"):
        renderings.append("compose")
    if target in ("both", "k8s") and config.k8s is not None:
        renderings.append("k8s")
    return renderings
