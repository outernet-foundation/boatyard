from __future__ import annotations

import shlex
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path

from boatyard.values_file import VALUES_FILE


def require_env_file(root: Path) -> None:
    if not (root / VALUES_FILE).exists():
        raise ValueError(f"{VALUES_FILE.as_posix()} is required")


def compose_command(
    root: Path,
    compose_files: list[Path],
    *arguments: str,
    env_file: Path | None = None,
    hole_names: Iterable[str] = (),
) -> str:
    # Spell the one docker compose invocation; the env -u scrub defeats compose's
    # shell-beats-env-file precedence for every hole name.
    quoted_arguments = [shlex.quote(argument) for argument in arguments]
    files_arguments = " ".join(f"-f {shlex.quote(str(compose_file))}" for compose_file in compose_files)
    env_argument = f" --env-file {shlex.quote(str(env_file))}" if env_file is not None else ""
    scrub = "".join(f" -u {shlex.quote(name)}" for name in sorted(set(hole_names)))
    prefix = f"docker compose {files_arguments} --project-directory {shlex.quote(str(root))}{env_argument}"
    if scrub:
        prefix = f"env{scrub} " + prefix
    return " ".join([prefix, *quoted_arguments])


@contextmanager
def lock_env_unwind(path: Path) -> Generator[None]:
    # Keep the env file on failure (path line only — its contents may carry
    # secrets), unlink it on success.
    try:
        yield
    except BaseException:
        print(f"env file kept for inspection: {path}")
        raise

    path.unlink(missing_ok=True)
