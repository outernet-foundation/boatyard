from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import NamedTuple, Self

from bashrun.bash import bash, bash_output
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DOCKERFILE_NAME = "Dockerfile"

FROM_LINE_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
ARG_LINE_PATTERN = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)")
COPY_FROM_LINE_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.IGNORECASE)
BUILD_ARG_REFERENCE_PATTERN = re.compile(r"^\$\{([A-Za-z0-9_]+)\}$")
AXIS_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
AXIS_VALUE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class Image(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dockerfile: str
    bases: dict[str, str] = Field(default_factory=dict)
    axis: str | None = None
    variants: list[str] = Field(default_factory=list)
    sha_args: list[str] = Field(default_factory=list)

    @property
    def contexts(self) -> list[str]:
        return sorted(set(self.bases.values()))

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


class BuiltBaseEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dockerfile: str
    axis: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("dockerfile")
    @classmethod
    def _require_repo_relative(cls, value: str) -> str:
        if not value or value.startswith("/") or ".." in PurePosixPath(value).parts:
            raise ValueError(f"dockerfile {value!r} must be a repo-relative path")
        return value

    @field_validator("axis")
    @classmethod
    def _validate_axis(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if len(value) > 1:
            raise ValueError("axis: declares at most one axis")
        for axis, values in value.items():
            if AXIS_NAME_PATTERN.fullmatch(axis) is None:
                raise ValueError(f"axis.{axis}: the name must be an ALL_CAPS env name")
            if not values:
                raise ValueError(f"axis.{axis}: must declare at least one value")
            seen: set[str] = set()
            for variant in values:
                if AXIS_VALUE_PATTERN.fullmatch(variant) is None:
                    raise ValueError(f"axis.{axis}: value {variant!r} must be a bare lowercase spelling like cuda")
                if variant in seen:
                    raise ValueError(f"axis.{axis}: value {variant!r} is declared twice")
                seen.add(variant)
        return value


class DockerfileFacts(NamedTuple):
    name: str
    dockerfile: str
    base_keys: list[str]
    args: list[str]


def load_images(
    root: Path,
    base_images: Mapping[str, str | BuiltBaseEntry],
    owned_images: Mapping[str, str],
    declared: Iterable[str],
) -> dict[str, Image]:
    # The build set is the closure over declared edges: built entries, workload source
    # annotations, and provisioner markers. Nothing walks the directory tree.
    built: dict[str, str] = {}
    for name, entry in base_images.items():
        if not isinstance(entry, BuiltBaseEntry):
            continue
        path = root / entry.dockerfile
        if not path.is_file():
            raise ValueError(f"base_images.{name}: {entry.dockerfile} does not exist")
        built[name] = PurePosixPath(entry.dockerfile).parent.name

    declared_paths = sorted(
        set(declared) | {entry.dockerfile for entry in base_images.values() if isinstance(entry, BuiltBaseEntry)}
    )
    directories: dict[str, Path] = {}
    sites: dict[str, str] = {}
    for dockerfile in declared_paths:
        if dockerfile.startswith("/") or ".." in PurePosixPath(dockerfile).parts:
            raise ValueError(f"{dockerfile}: a declared dockerfile path must be repo-relative")
        directory = (root / dockerfile).parent
        stem = PurePosixPath(dockerfile).parent.name
        if stem not in directories:
            directories[stem] = directory
            sites[stem] = dockerfile
        elif directories[stem] != directory:
            raise ValueError(f"{dockerfile}: the image stem {stem!r} is already claimed at {sites[stem]}")
    for stem, directory in directories.items():
        if not (directory / DOCKERFILE_NAME).is_file():
            raise ValueError(f"{sites[stem]}: the declared dockerfile does not exist")

    facts = {name: _parse_dockerfile(directory, root) for name, directory in sorted(directories.items())}

    # Every FROM key resolves against the declared vocabulary; built keys become base edges.
    resolvable = set(base_images) | set(owned_images) | set(built)
    bases: dict[str, dict[str, str]] = {}
    for name, fact in facts.items():
        bases[name] = {}
        for key in fact.base_keys:
            if key not in resolvable:
                raise ValueError(f"{fact.dockerfile}: FROM ${{{key}}} names no base_images key")
            if key in built:
                bases[name][key] = built[key]

    # One-hop grammar: a built base FROMs pulled entries only; a Dockerfile FROMs at most one built base.
    for name, fact in facts.items():
        own_bases = set(bases[name].values())
        if name in built.values() and own_bases:
            raise ValueError(f"{fact.dockerfile}: a built base must not FROM the built base {min(own_bases)!r}")
        if len(own_bases) > 1:
            raise ValueError(f"{fact.dockerfile}: FROMs more than one built base {sorted(own_bases)}")

    # A declared entry no Dockerfile consumes is dead.
    consumed = {stem for own_bases in bases.values() for stem in own_bases.values()}
    for name, stem in sorted(built.items()):
        if stem not in consumed:
            raise ValueError(f"base_images.{name}: no Dockerfile FROMs ${{{name}}} (dead declaration)")

    # The declared fan: the axis names an ARG in the built base's own Dockerfile; equal axis
    # names carry equal value sets across entries.
    axis_values: dict[str, list[str]] = {}
    axis_sites: dict[str, str] = {}
    entry_axes: dict[str, tuple[str, list[str]]] = {}
    for name, entry in base_images.items():
        if not isinstance(entry, BuiltBaseEntry):
            continue
        stem = built[name]
        for axis, values in entry.axis.items():
            if axis not in facts[stem].args:
                raise ValueError(f"base_images.{name}: axis {axis} names no ARG in {entry.dockerfile}")
            if axis in axis_values and axis_values[axis] != values:
                raise ValueError(
                    f"base_images.{name}: axis {axis} declares {values}; "
                    f"{axis_sites[axis]} already declares {axis_values[axis]}"
                )
            axis_values[axis] = values
            axis_sites[axis] = name
            entry_axes[stem] = (axis, list(values))

    images: dict[str, Image] = {}
    for name, fact in facts.items():
        axis: str | None = None
        variants: list[str] = []
        if name in entry_axes:
            axis, variants = entry_axes[name][0], list(entry_axes[name][1])
        else:
            [stem, *_] = [*bases[name].values(), None]
            if stem is not None and stem in entry_axes:
                axis, variants = entry_axes[stem][0], list(entry_axes[stem][1])
        images[name] = Image(
            name=fact.name,
            dockerfile=fact.dockerfile,
            bases=bases[name],
            axis=axis,
            variants=variants,
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
    base_keys: list[str] = []
    for image in from_images:
        if image in stages:
            continue
        arg_match = BUILD_ARG_REFERENCE_PATTERN.fullmatch(image)
        if arg_match is None:
            raise ValueError(f"{relative}: FROM {image} must be a ${{KEY}} reference to a base_images key")
        base_keys.append(arg_match.group(1))
    for value in copy_from_values:
        if "/" in value:
            raise ValueError(f"{relative}: COPY --from={value} must reference a base_images build arg")
    return DockerfileFacts(
        name=directory.name,
        dockerfile=relative,
        base_keys=base_keys,
        args=args,
    )
