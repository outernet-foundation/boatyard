from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer
from bashrun.bash import bash, bash_output

from boatyard import dockerfiles
from boatyard.config import Config, compose_bundle_output_path, lock_key_prefix
from boatyard.config_lock import LOCK_PATH, parse_config_lock
from boatyard.crane import install_crane
from boatyard.lock import env_entries
from boatyard.publish import (
    STUB_VALUE,
    VAR_PATTERN,
    PublishedEntry,
    bake_sources,
    finalize_baked,
)

app = typer.Typer()


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
    bundles = (
        [(name, compose_bundle_output_path(root, name)) for name, _entry in config.local_service_entries()]
        if config is not None
        else []
    )
    names = [name for name, _output in bundles]
    if config is None or not names:
        if plan:
            print(json.dumps([]))
            return
        raise ValueError("publishing requires local entries (workloads:) in boatyard.yml")

    if plan:
        entries = [{"bundle": name} for name in names]
        print(json.dumps(entries))
        return

    if bundle is not None and bundle not in names:
        raise typer.BadParameter(f"unknown --bundle {bundle!r}; declared bundles: {names}")

    username = os.environ.get("REGISTRY_USERNAME", "")
    token = os.environ.get("REGISTRY_TOKEN", "")
    if not username or not token:
        raise SystemExit("REGISTRY_USERNAME and REGISTRY_TOKEN must be set for registry login")
    repository = config.repository
    if repository is None:
        raise ValueError("local entries (workloads:) are declared; repository is required")
    registry_host = repository.partition("/")[0]
    print(f"Login to {registry_host}")
    bash(f"docker login {shlex.quote(registry_host)} -u {shlex.quote(username)} --password-stdin", stdin_text=token)
    install_crane()

    sha = bash_output("git rev-parse HEAD").strip()
    branch_tag = (branch or bash_output("git rev-parse --abbrev-ref HEAD").strip()).replace("/", "-")

    service_shas = dockerfiles.compute_service_shas(config.root, config.service_builds)
    lock_entries = env_entries(parse_config_lock(root / LOCK_PATH))

    published: list[PublishedEntry] = []
    for bundle_name, output in bundles:
        if bundle is not None and bundle_name != bundle:
            continue
        with TemporaryDirectory(prefix="compose-bundle-") as baked_directory_string:
            baked_directory = Path(baked_directory_string)
            # Fan the lock keys under the bundle prefix
            substitutions = {
                **service_shas,
                **lock_entries,
                **{lock_key_prefix(bundle_name) + key: value for key, value in lock_entries.items()},
            }
            baked_path = bake_sources(baked_directory, output.as_posix(), substitutions)

            # Set non-empty stubs for unresolved vars.
            unresolved = {
                match.group(1) for match in VAR_PATTERN.finditer(baked_path.read_text(encoding="utf-8"))
            } - substitutions.keys()
            for name in unresolved:
                os.environ.setdefault(name, STUB_VALUE)

            finalize_baked(baked_path, repository)

            # Publish once under the sha tag and alias the branch tag.
            sha_reference = f"{repository}/{bundle_name}:{sha}"
            print(f"Publish {sha_reference}")
            bash(f"docker compose -f {shlex.quote(str(baked_path))} publish {shlex.quote(sha_reference)} --yes")
            branch_reference = f"{repository}/{bundle_name}:{branch_tag}"
            print(f"Tag {branch_reference}")
            bash(f"crane tag {shlex.quote(sha_reference)} {shlex.quote(branch_tag)}")
            digest = bash_output(f"crane digest {shlex.quote(sha_reference)}").strip()
            print(f"Digest {sha_reference} {digest}")
            print(f"Digest {branch_reference} {digest}")
            published.append(PublishedEntry(bundle_name, sha_reference, branch_reference, digest))

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
