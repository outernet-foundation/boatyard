from __future__ import annotations

import re
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DOCKERFILE_NAME = "Dockerfile"
FROM_LINE_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
ARG_LINE_PATTERN = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)")
COPY_FROM_LINE_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.IGNORECASE)
BARE_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
BUILD_ARG_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z0-9_]+\}$")


class ServiceBuild(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    dockerfile: str
    contexts: list[str] = Field(default_factory=list)
    axis: str | None = None
    variants: list[str] = Field(default_factory=list)
    sha_args: list[str] = Field(default_factory=list)
    dev_stage: bool = False

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (self.axis is None) != (not self.variants):
            raise ValueError("axis and variants must be declared together")
        return self

    @property
    def target_variants(self) -> list[tuple[str, str | None]]:
        variants: list[str | None] = list(self.variants) or [None]
        return [(f"{self.name}-{variant}" if variant is not None else self.name, variant) for variant in variants]


def derive_services(root: Path, axes: dict[str, list[str]]) -> dict[str, ServiceBuild]:
    service_dirs: dict[str, Path] = {}
    tops: set[str] = set()
    for top in sorted(root.iterdir()):
        if not top.is_dir() or top.name.startswith("."):
            continue
        for child in sorted(top.iterdir()):
            if child.is_dir() and (child / DOCKERFILE_NAME).is_file():
                service_dirs[child.name] = child
                tops.add(top.name)
    require_single_top(tops, "Dockerfile service directories")

    builds: dict[str, ServiceBuild] = {}
    for name, directory in service_dirs.items():
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
        contexts: list[str] = []
        for image in from_images:
            if image in stages:
                continue
            if BARE_IDENTIFIER_PATTERN.fullmatch(image) is None:
                if BUILD_ARG_REFERENCE_PATTERN.fullmatch(image) is None:
                    raise RuntimeError(f"{relative}: FROM {image} must reference a base_images build arg")
                continue
            if image not in service_dirs:
                raise RuntimeError(f"{relative}: FROM {image} names no service directory or stage")
            contexts.append(image)
        for value in copy_from_values:
            if "/" in value:
                raise RuntimeError(f"{relative}: COPY --from={value} must reference a base_images build arg")
        declared_axis_args = [argument for argument in args if argument in axes]
        if len(declared_axis_args) > 1:
            raise RuntimeError(
                f"{relative}: declares two variant-axis ARGs ({', '.join(declared_axis_args)}); "
                "a service carries one axis"
            )
        axis_arg = declared_axis_args[0] if declared_axis_args else None
        builds[name] = ServiceBuild(
            name=name,
            dockerfile=relative,
            contexts=sorted(set(contexts)),
            axis=axis_arg,
            variants=list(axes[axis_arg]) if axis_arg is not None else [],
            sha_args=sorted({arg for arg in args if arg.endswith("_SHA")}),
            dev_stage="dev" in stages and len(stages) > 1,
        )

    effective_axes = {name: _effective_axis(name, builds, ()) for name in builds}
    for name, build in builds.items():
        axis = effective_axes[name]
        if axis is not None:
            build.axis = axis
            build.variants = list(axes[axis])
    return builds


def require_single_top(tops: set[str], subject: str) -> None:
    if len(tops) > 1:
        raise RuntimeError(f"{subject} span multiple top-level directories: {sorted(tops)}")


def _effective_axis(name: str, builds: dict[str, ServiceBuild], path: tuple[str, ...]) -> str | None:
    if name in path:
        raise RuntimeError(f"FROM context cycle: {' -> '.join((*path, name))}")
    build = builds[name]
    parent_axes = [_effective_axis(parent, builds, (*path, name)) for parent in build.contexts]
    if len(set(parent_axes)) > 1:
        raise RuntimeError(
            f"workloads/{name}/{DOCKERFILE_NAME}: parents ({', '.join(build.contexts)}) disagree on variant cells"
        )
    if parent_axes:
        if build.axis is not None:
            raise RuntimeError(
                f"workloads/{name}/{DOCKERFILE_NAME}: declares ARG {build.axis} over a "
                f"variant-bearing base (parents: {', '.join(build.contexts)})"
            )
        return parent_axes[0]
    return build.axis
