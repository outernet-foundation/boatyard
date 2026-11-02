from __future__ import annotations

import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, NamedTuple

import typer
from bashrun.bash import bash, bash_output

from docker_devkit.config import DockerDevkitConfig, Target
from docker_devkit.score import generate_artifacts

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


class ToolRequirement(NamedTuple):
    binary: str
    version: str
    release: str
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
        raise RuntimeError("--check and --local are mutually exclusive")
    root = Path.cwd()
    config = DockerDevkitConfig.load(root)
    bundles = config.compose_bundles() if config is not None else []
    if config is None or (not bundles and config.k8s is None):
        if not check:
            raise RuntimeError("docker-devkit.yaml declares no local service entries and no k8s section")
        print("no local service entries and no k8s section; nothing to check")
        return
    k8s_rendering = config.k8s is not None

    if not k8s_rendering and target == "k8s":
        raise typer.BadParameter("--target k8s: this config declares no k8s section (compose-only)")
    if not k8s_rendering and local:
        raise typer.BadParameter("--local requires a k8s section; this config declares none")

    # The pinned tools this run needs at exact versions.
    score_tools: list[tuple[str, str]] = []
    if target in ("both", "compose"):
        score_tools.append(("score-compose", config.score_compose_version))
    if target in ("both", "k8s") and config.k8s is not None:
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
    if config.k8s is not None and config.k8s.charts and target in ("both", "k8s"):
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
                archive = Path(requirement.archive).name
                bash(f"curl -fsSLO {requirement.archive}")
                bash(f"tar -xzf {archive} -C {bin_dir} {requirement.extract}")
                Path(archive).unlink()
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
            outputs = generate_artifacts(config, target, False)
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
