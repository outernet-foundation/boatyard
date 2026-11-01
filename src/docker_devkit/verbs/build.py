from __future__ import annotations

from typing import Annotated

import typer

from docker_devkit.bake import Gpu, run_build

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)


@app.command()
def build(
    push: Annotated[
        bool, typer.Option("--push", help="Push images (+ registry caches) and shelve the digest manifest.")
    ] = False,
    targets_opt: Annotated[
        list[str] | None,
        typer.Option("--targets", "-t", help="Build only these services (from the image manifest)."),
    ] = None,
    gpu: Annotated[Gpu, typer.Option("--gpu", help="auto|cuda|rocm|none")] = "auto",
    upgrade: bool = typer.Option(False, "--upgrade", "-u", help="Re-resolve and rewrite base digests."),
    lock_only: bool = typer.Option(False, "--lock-only", help="Update lock file without building images."),
    gpu_only: bool = typer.Option(
        False, "--gpu-only", help="Build only the services declaring this gpu family (requires a concrete --gpu)."
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help="Force rebuild by disabling cache usage."),
    dev: Annotated[
        bool, typer.Option("--dev", help="Build each Dockerfile-declared dev stage under the :dev tag (local-only).")
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="With --lock-only: assert the committed lock is fresh instead of writing it."),
    ] = False,
) -> None:
    targets = [service for value in targets_opt or [] for service in value.split()] or None
    run_build(
        push=push,
        targets_opt=targets,
        gpu=gpu,
        upgrade=upgrade,
        lock_only=lock_only,
        gpu_only=gpu_only,
        no_cache=no_cache,
        dev=dev,
        check=check,
    )
