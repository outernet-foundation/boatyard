from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from bashrun.bash import bash, bash_output

from boatyard.models.config_file import ConfigFile, Target
from boatyard.paths import DEFAULT_CONFIG_PATH
from boatyard.score_generate import generate_artifacts

app = typer.Typer()


@app.command()
def generate_score(
    target: Annotated[
        Target | None,
        typer.Option(help="Which Score artifact to generate; the default derives from the config."),
    ] = None,
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
    config_path = root / DEFAULT_CONFIG_PATH
    config = ConfigFile.load(config_path) if config_path.is_file() else None
    if config is None or not (config.local_service_entries() or config.k8s):
        if check:
            print("no local service entries and no k8s section; nothing to check")
            return
        raise ValueError("boatyard.yml declares no local service entries and no k8s section")

    outputs = generate_artifacts(config, target=target, local=local)

    if check:
        pathspec = " ".join(output.as_posix() for output in outputs)
        if bash_output(f"git status --porcelain -- {pathspec}").strip():
            bash(f"git diff -- {pathspec}")
            raise SystemExit(
                "score artifacts are stale. Run 'generate-score' and commit the result as 'Run generate-score'."
            )
