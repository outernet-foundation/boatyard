from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer
from bashrun.bash import bash, bash_output

from docker_devkit import dockerfiles
from docker_devkit.config import Config
from docker_devkit.config_lock import LOCK_PATH, parse_config_lock
from docker_devkit.crane import install_crane
from docker_devkit.lock import env_entries
from docker_devkit.publish import (
    STUB_VALUE,
    VAR_PATTERN,
    PublishedEntry,
    bake_sources,
    finalize_baked,
)

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def publish_compose(
    bundle: Annotated[
        str | None,
        typer.Option(
            "--bundle",
            help="Publish this bundle's entry (one artifact per workload-bearing entry or option); a bare invocation publishes every entry.",
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Branch tag for the second publish; defaults to the checked-out branch."),
    ] = None,
    plan: Annotated[
        bool,
        typer.Option(
            "--plan", help="Print the publish entries as a bare JSON array (empty when no section); no publish."
        ),
    ] = False,
) -> None:
    root = Path.cwd()
    config = Config.load(root)
    bundles = config.compose_bundles() if config is not None else []
    if config is None or not bundles:
        if plan:
            print(json.dumps([]))
            return
        raise RuntimeError("publishing requires local entries (workloads:) in docker-devkit.yaml")

    if plan:
        entries = [{"bundle": entry.name} for entry in bundles]
        print(json.dumps(entries))
        return

    names = [entry.name for entry in bundles]
    if bundle is not None and bundle not in names:
        raise typer.BadParameter(f"unknown --bundle {bundle!r}; declared bundles: {names}")

    username = os.environ.get("REGISTRY_USERNAME", "")
    token = os.environ.get("REGISTRY_TOKEN", "")
    if not username or not token:
        raise SystemExit("REGISTRY_USERNAME and REGISTRY_TOKEN must be set for registry login")
    repository = config.repository
    if repository is None:
        raise RuntimeError("local entries (workloads:) are declared; repository is required")
    registry_host = repository.partition("/")[0]
    print(f"Login to {registry_host}")
    bash(f"docker login {shlex.quote(registry_host)} -u {shlex.quote(username)} --password-stdin", stdin_text=token)
    install_crane()

    sha = bash_output("git rev-parse HEAD").strip()
    branch_tag = (branch or bash_output("git rev-parse --abbrev-ref HEAD").strip()).replace("/", "-")

    substitutions = {
        **dockerfiles.compute_service_shas(config.root, config.service_builds),
        **env_entries(parse_config_lock(root / LOCK_PATH)),
    }

    published: list[PublishedEntry] = []
    for entry in bundles:
        if bundle is not None and entry.name != bundle:
            continue
        with TemporaryDirectory(prefix="compose-bundle-") as baked_directory_string:
            baked_directory = Path(baked_directory_string)
            baked_path = bake_sources(baked_directory, entry.output.as_posix(), substitutions)

            # Set non-empty stubs for unresolved vars.
            unresolved = {
                match.group(1) for match in VAR_PATTERN.finditer(baked_path.read_text(encoding="utf-8"))
            } - substitutions.keys()
            for name in unresolved:
                os.environ.setdefault(name, STUB_VALUE)

            finalize_baked(baked_path, repository)

            # Publish once under the sha tag and alias the branch tag.
            sha_reference = f"{repository}/{entry.name}:{sha}"
            print(f"Publish {sha_reference}")
            bash(f"docker compose -f {shlex.quote(str(baked_path))} publish {shlex.quote(sha_reference)} --yes")
            branch_reference = f"{repository}/{entry.name}:{branch_tag}"
            print(f"Tag {branch_reference}")
            bash(f"crane tag {shlex.quote(sha_reference)} {shlex.quote(branch_tag)}")
            digest = bash_output(f"crane digest {shlex.quote(sha_reference)}").strip()
            print(f"Digest {sha_reference} {digest}")
            print(f"Digest {branch_reference} {digest}")
            published.append(PublishedEntry(entry.name, sha_reference, branch_reference, digest))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path is not None and published:
        lines = [
            "## Published compose bundles",
            "",
            "| artifact | sha tag | branch tag | digest |",
            "| --- | --- | --- | --- |",
        ]
        for entry in published:
            lines.append(
                f"| {entry.artifact} | `{entry.sha_reference}` | `{entry.branch_reference}` | `{entry.digest}` |"
            )
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")
