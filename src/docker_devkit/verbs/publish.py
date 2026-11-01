from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer
from bashrun.bash import bash, bash_output

from docker_devkit.config import LOCK_PATH, load_config, parse_image_lock
from docker_devkit.crane import install_crane
from docker_devkit.publish import (
    STUB_VALUE,
    VAR_PATTERN,
    PublishLeg,
    PublishedLeg,
    bake_sources,
    finalize_baked,
)
from docker_devkit.service_builds import compute_service_shas

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def publish_compose(
    bundle: Annotated[
        str | None,
        typer.Option(
            "--bundle",
            help="Publish this bundle leg (one leg per bundle x gpu variant); a bare invocation publishes every leg.",
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Branch tag for the second publish; defaults to the checked-out branch."),
    ] = None,
    plan: Annotated[
        bool,
        typer.Option("--plan", help="Print the publish legs as a bare JSON array (empty when no section); no publish."),
    ] = False,
) -> None:
    root = Path.cwd()
    config = load_config(root)
    if config is None or not config.compose_bundles():
        if plan:
            print(json.dumps([]))
            return
        raise RuntimeError(
            "publishing happens iff local service entries are declared in docker-devkit.yaml; "
            "one artifact per workload-bearing entry or option, named under root repository"
        )

    bundles = config.compose_bundles()

    if plan:
        legs = [{"bundle": entry.name} for entry in bundles]
        print(json.dumps(legs))
        return

    names = [entry.name for entry in bundles]
    if bundle is not None and bundle not in names:
        raise typer.BadParameter(f"unknown --bundle {bundle!r}; declared bundles: {names}")
    selected = [
        PublishLeg(entry.name, [entry.output.as_posix()]) for entry in bundles if bundle is None or entry.name == bundle
    ]

    username = os.environ.get("REGISTRY_USERNAME", "")
    token = os.environ.get("REGISTRY_TOKEN", "")
    if not username or not token:
        raise SystemExit("REGISTRY_USERNAME and REGISTRY_TOKEN must be set for registry login")
    if config.repository is None:
        raise RuntimeError("bundles are declared; root repository names the artifact registry (ghcr.io/org/repo)")
    registry_host = config.repository.partition("/")[0]
    print(f"Login to {registry_host}")
    bash(f"docker login {shlex.quote(registry_host)} -u {shlex.quote(username)} --password-stdin", stdin_text=token)
    install_crane()

    sha = bash_output("git rev-parse HEAD").strip()
    branch_tag = (branch or bash_output("git rev-parse --abbrev-ref HEAD").strip()).replace("/", "-")

    substitutions = {
        **compute_service_shas(root, config.service_builds),
        **parse_image_lock(root / LOCK_PATH).root,
    }

    published: list[PublishedLeg] = []
    for leg in selected:
        with TemporaryDirectory(prefix="compose-bundle-") as baked_directory_string:
            baked_directory = Path(baked_directory_string)
            baked_paths = bake_sources(baked_directory, leg.files, substitutions)

            # Publish validates ${VAR} holes against the environment but embeds the file
            # uninterpolated (verified against a live registry — no env value enters the
            # artifact), so stubs exist only to satisfy that validation; they must be
            # non-empty because compose's ${VAR:?err} rejects unset OR empty.
            unresolved = {
                match.group(1)
                for baked_path in baked_paths
                for match in VAR_PATTERN.finditer(baked_path.read_text(encoding="utf-8"))
            } - substitutions.keys()
            for name in unresolved:
                os.environ.setdefault(name, STUB_VALUE)

            finalize_baked(baked_paths, config.repository)

            compose_files = " ".join(f"-f {shlex.quote(str(baked_path))}" for baked_path in baked_paths)
            # One publish per leg: compose stamps wall-clock created per invocation, so a
            # second publish of identical files yields a different digest — the branch tag
            # aliases the published manifest instead of re-publishing it.
            sha_reference = f"{config.repository}/{leg.artifact}:{sha}"
            print(f"Publish {sha_reference}")
            bash(f"docker compose {compose_files} publish {shlex.quote(sha_reference)} --yes")
            branch_reference = f"{config.repository}/{leg.artifact}:{branch_tag}"
            print(f"Tag {branch_reference}")
            bash(f"crane tag {shlex.quote(sha_reference)} {shlex.quote(branch_tag)}")
            digest = bash_output(f"crane digest {shlex.quote(sha_reference)}").strip()
            print(f"Digest {sha_reference} {digest}")
            print(f"Digest {branch_reference} {digest}")
            published.append(PublishedLeg(leg.artifact, sha_reference, branch_reference, digest))

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
