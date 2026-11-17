from __future__ import annotations

import shlex
import sys
from collections.abc import Iterable
from pathlib import Path


def compose_command(
    root: Path,
    compose_files: list[Path],
    *arguments: str,
    project_name: str,
    env_file: Path | None = None,
    slot_names: Iterable[str] = (),
) -> str:
    # Spell the one docker compose invocation; the env -u scrub defeats compose's
    # shell-beats-env-file precedence for every slot name.
    quoted_arguments = [_quote(argument) for argument in arguments]
    files_argument = "".join(f" -f {_quote(str(compose_file))}" for compose_file in compose_files)
    env_argument = f" --env-file {_quote(str(env_file))}" if env_file is not None else ""
    scrub = "".join(f" -u {_quote(name)}" for name in sorted(set(slot_names)))
    prefix = (
        f"docker compose -p {_quote(project_name)}{files_argument} "
        f"--project-directory {_quote(str(root))}{env_argument}"
    )
    # Windows ships no env utility; the scrub stays the POSIX spelling of the guard.
    if scrub and sys.platform != "win32":
        prefix = f"env{scrub} " + prefix
    return " ".join([prefix, *quoted_arguments])


def _quote(value: str) -> str:
    # bashrun splits the command without posix quoting on Windows, where shlex.quote's
    # single quotes would ride the argument literally.
    if sys.platform == "win32":
        return value
    return shlex.quote(value)
