from __future__ import annotations

import shlex
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
    quoted_arguments = [shlex.quote(argument) for argument in arguments]
    files_argument = "".join(f" -f {shlex.quote(str(compose_file))}" for compose_file in compose_files)
    env_argument = f" --env-file {shlex.quote(str(env_file))}" if env_file is not None else ""
    scrub = "".join(f" -u {shlex.quote(name)}" for name in sorted(set(slot_names)))
    prefix = (
        f"docker compose -p {shlex.quote(project_name)}{files_argument} "
        f"--project-directory {shlex.quote(str(root))}{env_argument}"
    )
    if scrub:
        prefix = f"env{scrub} " + prefix
    return " ".join([prefix, *quoted_arguments])
