from __future__ import annotations

import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer
from bashrun.bash import bash, bash_output

from docker_devkit.config import Target, load_config
from docker_devkit.score import generate_artifacts

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


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
        raise RuntimeError(
            "--check verifies the committed artifacts; it cannot run against the gitignored --local output"
        )
    root = Path.cwd()
    config = load_config(root)
    bundles = config.compose_bundles() if config is not None else []
    if config is None or (not bundles and config.k8s is None):
        if not check:
            raise RuntimeError(
                "docker-devkit.yaml declares no local service entries and no k8s section; nothing to generate"
            )
        print("no local service entries and no k8s section; nothing to check")
        return
    k8s_rendering = config.k8s is not None

    if not k8s_rendering and target == "k8s":
        raise typer.BadParameter("--target k8s: this config declares no k8s section (compose-only)")
    if not k8s_rendering and local:
        raise typer.BadParameter("--local writes the k8s artifact; this config declares no k8s section")

    if check:
        # Fetch the pinned tools rather than trusting whatever PATH holds, so the gate always
        # regenerates with the exact versions the consumer pinned. Release binaries rather
        # than go install, since score-spec tags are not valid Go module versions.
        with TemporaryDirectory(prefix="score-tools-") as temporary_directory:
            bin_dir = Path(temporary_directory) / "bin"
            bin_dir.mkdir()
            for requirement in config.tool_requirements(target):
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

    # Refuse to run without the exact pinned tools, since output differs between releases.
    missing = [item for item in config.tool_requirements(target) if shutil.which(item.binary) is None]
    if missing:
        lines = [f"  {item.binary} {item.version}: {item.release}" for item in missing]
        raise RuntimeError(
            "generate-score needs these on PATH, at these exact versions (output differs between releases):\n"
            + "\n".join(lines)
        )
    generate_artifacts(config, target, local)
