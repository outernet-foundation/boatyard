from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal, NamedTuple, get_args

import pathspec
from bashrun.bash import bash, bash_output
from pydantic import BaseModel, ConfigDict, Field

GpuFamily = Literal["cuda", "rocm"]

DOCKERFILE_NAME = "Dockerfile"
FAN_ARG = "TORCH_DEVICE"
FROM_LINE_PATTERN = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
ARG_LINE_PATTERN = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)")
COPY_FROM_LINE_PATTERN = re.compile(r"^COPY\s+--from=(\S+)", re.IGNORECASE)
BARE_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
BUILD_ARG_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z0-9_]+\}$")


def service_sha_env(name: str) -> str:
    return name.upper().replace("-", "_") + "_SHA"


class ServiceBuild(BaseModel):
    # Derived by load_config from the Dockerfile tree; never authored.
    model_config = ConfigDict(extra="forbid")

    name: str
    dockerfile: str
    contexts: list[str] = Field(default_factory=list)
    variants: list[GpuFamily] = Field(default_factory=list)
    sha_args: list[str] = Field(default_factory=list)
    dev_stage: bool = False

    @property
    def target_variants(self) -> list[tuple[str, GpuFamily | None]]:
        variants: list[GpuFamily | None] = list(self.variants) or [None]
        return [(f"{self.name}-{variant}" if variant is not None else self.name, variant) for variant in variants]

    @property
    def targets(self) -> list[str]:
        return [target for target, _variant in self.target_variants]


class ParsedDockerfile(NamedTuple):
    # The pre-resolution parse of one service Dockerfile; the gpu-variant graph resolves
    # over these records before any ServiceBuild is constructed.
    name: str
    dockerfile: str
    contexts: list[str]
    sha_args: list[str]
    dev_stage: bool
    own_fan: bool


def derive_services(root: Path) -> dict[str, ServiceBuild]:
    # Every Dockerfile-bearing directory one level below a top-level directory is a
    # service; the graph (context edges, gpu fans, SHA args) derives from the
    # Dockerfiles themselves. Parsing and variant resolution complete before any
    # ServiceBuild is constructed — each record is built once, fully resolved.
    parsed = _parse_dockerfiles(root)

    computable = {service_sha_env(name) for name in parsed}
    for service in parsed.values():
        unknown = [arg for arg in service.sha_args if arg not in computable]
        if unknown:
            raise RuntimeError(
                f"workloads/{service.name}/{DOCKERFILE_NAME}: ARG {', '.join(unknown)} names no derived service; "
                "the stamp receiver must match a service directory"
            )

    resolved_variants: dict[str, list[GpuFamily]] = {}
    return {
        name: ServiceBuild(
            name=name,
            dockerfile=service.dockerfile,
            contexts=service.contexts,
            variants=_resolve_variants(name, (), parsed, resolved_variants),
            sha_args=service.sha_args,
            dev_stage=service.dev_stage,
        )
        for name, service in parsed.items()
    }


def _parse_dockerfiles(root: Path) -> dict[str, ParsedDockerfile]:
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

    parsed: dict[str, ParsedDockerfile] = {}
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
                    raise RuntimeError(
                        f"{relative}: FROM {image} is a hand-written image reference; "
                        "declare the base under base_images and FROM its build arg"
                    )
                continue
            if image not in service_dirs:
                raise RuntimeError(
                    f"{relative}: FROM {image} names no service directory; "
                    "a bare identifier in FROM must name a sibling service or a stage"
                )
            contexts.append(image)
        for value in copy_from_values:
            if "/" in value:
                raise RuntimeError(
                    f"{relative}: COPY --from={value} is a hand-written image reference; "
                    "declare the base under base_images and COPY from its build arg"
                )
        parsed[name] = ParsedDockerfile(
            name=name,
            dockerfile=dockerfile.relative_to(root).as_posix(),
            contexts=sorted(set(contexts)),
            sha_args=sorted({arg for arg in args if arg.endswith("_SHA")}),
            # A genuine dev stage is one of several stages; a single-stage AS dev label
            # names the published image and is not a dev shape.
            dev_stage="dev" in stages and len(stages) > 1,
            own_fan=FAN_ARG in args,
        )
    return parsed


def require_single_top(tops: set[str], subject: str) -> None:
    if len(tops) > 1:
        raise RuntimeError(f"{subject} span multiple top-level directories: {sorted(tops)}")


def _resolve_variants(
    name: str,
    seen: tuple[str, ...],
    parsed: dict[str, ParsedDockerfile],
    resolved: dict[str, list[GpuFamily]],
) -> list[GpuFamily]:
    if name in resolved:
        return resolved[name]
    if name in seen:
        raise RuntimeError(f"FROM context cycle: {' -> '.join((*seen, name))}")
    parent_sets = [_resolve_variants(parent, (*seen, name), parsed, resolved) for parent in parsed[name].contexts]
    distinct = {tuple(variants) for variants in parent_sets}
    if len(distinct) > 1:
        parents = ", ".join(parsed[name].contexts)
        raise RuntimeError(
            f"workloads/{name}/{DOCKERFILE_NAME}: parents ({parents}) disagree on gpu variants; "
            "a dependent inherits its base's variant set"
        )
    if parent_sets:
        if parsed[name].own_fan:
            raise RuntimeError(
                f"workloads/{name}/{DOCKERFILE_NAME}: declares ARG {FAN_ARG} over a fanned base; "
                "a dependent inherits its base's variant set"
            )
        variants = parent_sets[0]
    else:
        variants = list(get_args(GpuFamily)) if parsed[name].own_fan else []
    resolved[name] = variants
    return variants


def compute_service_shas(repo_root: Path, service_builds: dict[str, ServiceBuild]) -> dict[str, str]:
    repo_root = repo_root.resolve()

    service_dirs = {str(PurePosixPath(service.dockerfile).parent) for service in service_builds.values()}

    if not service_dirs:
        # A repo with no Dockerfile services (mirror-only, or bundles built elsewhere)
        # has nothing to hash; skip the git and .dockerignore reads entirely.
        return {}

    # Derive the workload root as the single top-level directory holding every service;
    # files under it that belong to no service are excluded from every SHA.
    tops = {PurePosixPath(service_dir).parts[0] for service_dir in service_dirs if PurePosixPath(service_dir).parts}
    require_single_top(tops, "services")
    workload_root = PurePosixPath(next(iter(tops))) if tops else None

    tree_entries = bash_output("git ls-tree -r HEAD", cwd=repo_root).splitlines()

    dockerignore = repo_root / ".dockerignore"
    spec = pathspec.PathSpec.from_lines("gitignore", dockerignore.read_text().splitlines())

    allowed_entries: list[tuple[str, str, str]] = []
    for entry in tree_entries:
        meta, path = entry.split("\t", 1)
        if spec.match_file(path):
            continue
        mode, _type, obj_hash = meta.split()
        allowed_entries.append((mode, obj_hash, path))

    workload_prefix = workload_root.as_posix() + "/" if workload_root is not None else None
    shared_entries: list[tuple[str, str, str]] = []
    per_service: dict[str, list[tuple[str, str, str]]] = {directory: [] for directory in service_dirs}

    for mode, obj_hash, path in allowed_entries:
        if workload_prefix is not None and path.startswith(workload_prefix):
            for service_dir in service_dirs:
                if path.startswith(service_dir + "/"):
                    per_service[service_dir].append((mode, obj_hash, path))
                    break
        else:
            shared_entries.append((mode, obj_hash, path))

    result: dict[str, str] = {}
    for service_dir in sorted(service_dirs):
        entries = shared_entries + per_service[service_dir]
        index_input = "\n".join(f"{mode} {obj_hash}\t{path}" for mode, obj_hash, path in entries) + "\n"

        with TemporaryDirectory() as tmpdir:
            index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}
            bash("git update-index --index-info", cwd=repo_root, env=index_env, stdin_text=index_input)
            tree_hash = bash_output("git write-tree", cwd=repo_root, env=index_env).strip()

        result[service_sha_env(PurePosixPath(service_dir).name)] = f"tree-{tree_hash}"

    return result
