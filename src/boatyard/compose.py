from __future__ import annotations

import shlex
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from boatyard.values_file import ENV_FILE


def require_env_file(root: Path) -> None:
    if not (root / ENV_FILE).exists():
        raise ValueError(".env is required")


def compose_command(
    root: Path,
    compose_files: list[Path],
    *arguments: str,
    env_file: Path | None = None,
) -> str:
    # Spell the one docker compose invocation.
    quoted_arguments = [shlex.quote(argument) for argument in arguments]
    files_arguments = " ".join(f"-f {shlex.quote(str(compose_file))}" for compose_file in compose_files)
    env_argument = f" --env-file {shlex.quote(str(env_file))}" if env_file is not None else ""
    prefix = (
        f"docker compose {files_arguments} --project-directory {shlex.quote(str(root))} --env-file .env{env_argument}"
    )
    return " ".join([prefix, *quoted_arguments])


@contextmanager
def lock_env_unwind(path: Path) -> Generator[None]:
    # Print and keep the env file on failure, unlink it on success.
    try:
        yield
    except BaseException:
        print(f"lock env file kept for inspection: {path}")
        print(path.read_text(encoding="utf-8"), end="")
        raise

    path.unlink(missing_ok=True)
