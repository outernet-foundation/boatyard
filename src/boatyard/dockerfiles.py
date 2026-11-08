from __future__ import annotations

from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Self

from bashrun.bash import bash, bash_output
from pydantic import BaseModel, ConfigDict, Field, model_validator

DOCKERFILE_NAME = "Dockerfile"


class ServiceBuild(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dockerfile: str
    contexts: list[str] = Field(default_factory=list)
    axis: str | None = None
    variants: list[str] = Field(default_factory=list)
    sha_args: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (self.axis is None) != (not self.variants):
            raise ValueError("axis and variants must be declared together")
        return self

    @property
    def target_variants(self) -> list[tuple[str, str | None]]:
        variants: list[str | None] = list(self.variants) or [None]
        return [(f"{self.name}-{variant}" if variant is not None else self.name, variant) for variant in variants]


def require_single_top(tops: set[str], subject: str) -> None:
    if len(tops) > 1:
        raise ValueError(f"{subject} span multiple top-level directories: {sorted(tops)}")


def service_sha_env(name: str) -> str:
    return name.upper().replace("-", "_") + "_SHA"


def compute_service_shas(repo_root: Path, service_builds: dict[str, ServiceBuild]) -> dict[str, str]:
    repo_root = repo_root.resolve()

    service_dirs = {str(PurePosixPath(service.dockerfile).parent) for service in service_builds.values()}

    if not service_dirs:
        # Skip the disk walk when no services exist.
        return {}

    # Derive the workload root as the single top-level directory holding every service.
    tops = {PurePosixPath(service_dir).parts[0] for service_dir in service_dirs}
    require_single_top(tops, "services")
    workload_prefix = f"{next(iter(tops))}/"

    # Require that .dockerignore be an allowlist
    rules = [line.strip() for line in (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    if sum(1 for rule in rules if rule == "*") != 1:
        raise ValueError(".dockerignore: must contain exactly one '*' line")
    includes: list[str] = []
    for number, rule in enumerate(rules, start=1):
        if not rule or rule.startswith("#") or rule == "*":
            continue
        if not rule.startswith("!"):
            raise ValueError(
                f".dockerignore:{number}: only allowlists are supported ('*' + '!path' lines); got {rule!r}"
            )
        path = rule[1:].strip("/")
        if not path or any(character in path for character in "*?[]\\"):
            raise ValueError(
                f".dockerignore:{number}: re-include lines must be literal paths without wildcards; got {rule!r}"
            )
        includes.append(path)

    # Collect allowlisted files present on disk
    disk_files = [
        path
        for path in (entry.relative_to(repo_root).as_posix() for entry in repo_root.rglob("*") if entry.is_file())
        if any(path == include or path.startswith(include + "/") for include in includes)
    ]

    # Keep only git-visible files (tracked and untracked-not-ignored); ignored files never hash.
    visible = set(
        bash_output(
            "git -c core.excludesFile=/dev/null ls-files --cached --others --exclude-standard -z",
            cwd=repo_root,
            env={"GIT_CONFIG_NOSYSTEM": "1"},
        ).split("\0")
    )

    allowed = [path for path in disk_files if path in visible]

    # Partition into shared and per-service paths, dropping workload-root files owned by no service.
    shared_paths = [path for path in allowed if not path.startswith(workload_prefix)]
    per_service = {d: [p for p in allowed if p.startswith(d + "/")] for d in service_dirs}

    result: dict[str, str] = {}
    for service_dir in sorted(service_dirs):
        with TemporaryDirectory() as tmpdir:
            index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
            # Conversion pinned off so blobs hash the raw on-disk bytes; git derives modes and symlinks from disk.
            bash(
                "git -c core.autocrlf=false update-index --add --stdin",
                cwd=repo_root,
                env=index_env,
                stdin_text="".join(f"{path}\n" for path in shared_paths + per_service[service_dir]),
            )
            tree_hash = bash_output("git write-tree", cwd=repo_root, env=index_env).strip()

        result[service_sha_env(PurePosixPath(service_dir).name)] = f"tree-{tree_hash}"

    return result
