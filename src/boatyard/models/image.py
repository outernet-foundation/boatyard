from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import NamedTuple, Self

from bashrun.bash import bash, bash_output
from pydantic import BaseModel, ConfigDict, Field, model_validator

DOCKERFILE_NAME = "Dockerfile"

FROM_LINE_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
ARG_LINE_PATTERN = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)")
COPY_FROM_LINE_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.IGNORECASE)
BARE_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
BUILD_ARG_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z0-9_]+\}$")


class Image(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dockerfile: str
    contexts: list[str] = Field(default_factory=list)
    axis: str | None = None
    variants: list[str] = Field(default_factory=list)
    sha_args: list[str] = Field(default_factory=list)

    def compute_sha(self, repo_root: Path) -> str:
        repo_root = repo_root.resolve()
        image_directory = PurePosixPath(self.dockerfile).parent
        image_root_prefix = f"{image_directory.parts[0]}/"

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

        # The hashed set is everything allowlisted outside the image root plus this image's own directory.
        shared_paths = [path for path in allowed if not path.startswith(image_root_prefix)]
        own_paths = [path for path in allowed if path.startswith(f"{image_directory}/")]

        with TemporaryDirectory() as tmpdir:
            index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
            # Conversion pinned off so blobs hash the raw on-disk bytes; git derives modes and symlinks from disk.
            bash(
                "git -c core.autocrlf=false update-index --add --stdin",
                cwd=repo_root,
                env=index_env,
                stdin_text="".join(f"{path}\n" for path in shared_paths + own_paths),
            )
            tree_hash = bash_output("git write-tree", cwd=repo_root, env=index_env).strip()

        return f"tree-{tree_hash}"

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (self.axis is None) != (not self.variants):
            raise ValueError("axis and variants must be declared together")
        return self

    @property
    def target_variants(self) -> list[tuple[str, str | None]]:
        variants: list[str | None] = list(self.variants) or [None]
        return [(f"{self.name}-{variant}" if variant is not None else self.name, variant) for variant in variants]

    @property
    def sha_env(self) -> str:
        return self.name.upper().replace("-", "_") + "_SHA"


class DockerfileFacts(NamedTuple):
    name: str
    dockerfile: str
    context_candidates: list[str]
    args: list[str]


def load_images(root: Path, axes: Mapping[str, list[str]]) -> dict[str, Image]:
    directories: dict[str, Path] = {}
    tops: set[str] = set()
    for top in sorted(root.iterdir()):
        if not top.is_dir() or top.name.startswith("."):
            continue
        for child in sorted(top.iterdir()):
            if child.is_dir() and (child / DOCKERFILE_NAME).is_file():
                directories[child.name] = child
                tops.add(top.name)
    if len(tops) > 1:
        raise ValueError(f"Dockerfile image directories span multiple top-level directories: {sorted(tops)}")

    facts = {name: _parse_dockerfile(directory, root) for name, directory in sorted(directories.items())}

    contexts: dict[str, list[str]] = {}
    for name, fact in facts.items():
        for candidate in fact.context_candidates:
            if candidate not in facts:
                raise ValueError(f"{fact.dockerfile}: FROM {candidate} names no image directory or stage")
        contexts[name] = sorted(set(fact.context_candidates))

    own_axes: dict[str, str | None] = {}
    for name, fact in facts.items():
        axis_args = [argument for argument in fact.args if argument in axes]
        own_axes[name] = axis_args[0] if axis_args else None

    images: dict[str, Image] = {}
    for name, fact in facts.items():
        axis = _effective_axis(name, contexts, own_axes)
        images[name] = Image(
            name=fact.name,
            dockerfile=fact.dockerfile,
            contexts=contexts[name],
            axis=axis,
            variants=list(axes[axis]) if axis is not None else [],
            sha_args=sorted({argument for argument in fact.args if argument.endswith("_SHA")}),
        )

    computable = {image.sha_env for image in images.values()}
    for image in images.values():
        unknown = [argument for argument in image.sha_args if argument not in computable]
        if unknown:
            raise ValueError(f"{image.dockerfile}: ARG {', '.join(unknown)} names no image directory")
    return images


def _parse_dockerfile(directory: Path, root: Path) -> DockerfileFacts:
    dockerfile = directory / DOCKERFILE_NAME
    relative = dockerfile.relative_to(root).as_posix()
    from_images: list[str] = []
    copy_from_values: list[str] = []
    stages: set[str] = set()
    args: list[str] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        from_match = FROM_LINE_PATTERN.match(line.strip())
        if from_match is not None:
            from_images.append(from_match.group(1))
            if from_match.group(2) is not None:
                stages.add(from_match.group(2))
            continue
        copy_from_match = COPY_FROM_LINE_PATTERN.match(line.strip())
        if copy_from_match is not None:
            copy_from_values.append(copy_from_match.group(1))
            continue
        arg_match = ARG_LINE_PATTERN.match(line.strip())
        if arg_match is not None:
            args.append(arg_match.group(1))
    context_candidates: list[str] = []
    for image in from_images:
        if image in stages:
            continue
        if BARE_IDENTIFIER_PATTERN.fullmatch(image) is None:
            if BUILD_ARG_REFERENCE_PATTERN.fullmatch(image) is None:
                raise ValueError(f"{relative}: FROM {image} must reference a base_images build arg")
            continue
        context_candidates.append(image)
    for value in copy_from_values:
        if "/" in value:
            raise ValueError(f"{relative}: COPY --from={value} must reference a base_images build arg")
    return DockerfileFacts(
        name=directory.name,
        dockerfile=relative,
        context_candidates=context_candidates,
        args=args,
    )


def _effective_axis(name: str, contexts: Mapping[str, list[str]], own_axes: Mapping[str, str | None]) -> str | None:
    if contexts[name]:
        return _effective_axis(contexts[name][0], contexts, own_axes)
    return own_axes[name]
