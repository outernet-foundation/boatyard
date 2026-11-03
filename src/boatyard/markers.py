from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict

from boatyard.config import Yaml, is_mapping

TEXT_MARKER = "include"
YAML_MARKER = "include-yaml"
LOCK_MARKER = "lock"
LOCK_IMAGE_MARKER = "lock-image"
LOCK_MARKERS = (LOCK_MARKER, LOCK_IMAGE_MARKER)
IMAGE_FIELD = "image"
IMAGE_HOLE_PATTERN = re.compile(r"\$\{|{{")


class LockImageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    digest_field: Literal["digest", "sha"] = "digest"


class MarkerContext(NamedTuple):
    root: Path
    images: dict[str, str]
    source: str
    ancestors: tuple[Path, ...] = ()


class ImageReference(NamedTuple):
    registry: str
    repository: str
    tag: str | None
    digest: str


def resolve_markers(node: Yaml, context: MarkerContext) -> Yaml:
    # Includes splice in structure that may itself carry lock markers, so includes resolve first.
    return resolve_locks(expand_includes(node, context), context)


def expand_includes(node: Yaml, context: MarkerContext) -> Yaml:
    match node:
        case list():
            # Resolve every element of a list.
            expanded = [expand_includes(item, context) for item in node]
        case dict():
            match tuple(node.items()):
                # A single-key include marker splices in file content.
                case [(marker, value)] if marker == TEXT_MARKER:
                    expanded = include_text(value, context)
                case [(marker, value)] if marker == YAML_MARKER:
                    expanded = include_yaml(value, context)
                # A lock marker owns its value whole; the lock pass consumes or refuses it.
                case [(marker, _)] if marker in LOCK_MARKERS:
                    expanded = node
                # Any other mapping resolves child by child.
                case entries:
                    expanded = {key: expand_includes(child, context) for key, child in entries}
        case _:
            # Scalars pass through untouched.
            expanded = node

    return expanded


def include_text(value: Yaml, context: MarkerContext) -> str:
    path = included_path(value, TEXT_MARKER, context)
    return path.read_text(encoding="utf-8")


def include_yaml(value: Yaml, context: MarkerContext) -> Yaml:
    path = included_path(value, YAML_MARKER, context)

    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"{context.source}: include {value!r} is not valid YAML: {error}") from error

    if parsed is None:
        raise ValueError(f"{context.source}: include {value!r} is empty")

    return expand_includes(parsed, context._replace(ancestors=(*context.ancestors, path)))


def included_path(value: Yaml, marker: str, context: MarkerContext) -> Path:
    source = context.source

    match value:
        case str():
            relative = Path(value)
        case _:
            raise ValueError(f"{source}: {marker} takes a repo-root-relative path string, not {type(value).__name__}")

    # Anchor include paths at the repo root.
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{source}: include path {value!r} must anchor at the repo root")

    path = context.root / relative

    # Refuse cycles and missing targets.
    if path in context.ancestors:
        raise ValueError(f"{source}: include {value!r} participates in a cycle")
    if not path.is_file():
        raise ValueError(f"{source}: include {value!r} does not exist at the repo root")

    return path


def resolve_locks(node: Yaml, context: MarkerContext) -> Yaml:
    match node:
        case list():
            # Resolve every element of a list.
            resolved = [resolve_locks(item, context) for item in node]
        case dict():
            match tuple(node.items()):
                # A single-key lock marker substitutes the locked reference as one string.
                case [(marker, value)] if marker == LOCK_MARKER:
                    resolved = locked_reference(value, context)
                case [(marker, value)] if marker == LOCK_IMAGE_MARKER:
                    resolved = lock_image_fields(value, context.images, context.source)
                # A plain mapping refuses literal images, then resolves child by child.
                case entries:
                    _check_literal_image(node, context.source)
                    resolved = {key: resolve_locks(child, context) for key, child in entries}
        case _:
            # Scalars pass through untouched.
            resolved = node

    return resolved


def locked_reference(value: Yaml, context: MarkerContext) -> str:
    match value:
        case str() if value in context.images:
            reference = context.images[value]
        case str():
            raise ValueError(f"{context.source}: lock entry {value!r} does not exist in the image lock")
        case _:
            raise ValueError(f"{context.source}: lock takes a lock-entry name string, not {type(value).__name__}")

    return reference


def lock_image_fields(value: Yaml, images: dict[str, str], source: str) -> dict[str, Yaml]:
    # A bare string names the lock entry; a mapping spells key and digest_field.
    if isinstance(value, str):
        image_spec = LockImageSpec(key=value)
    elif is_mapping(value):
        image_spec = LockImageSpec.model_validate(value)
    else:
        raise ValueError(
            f"{source}: lock-image takes a lock-entry name or a key/digest_field mapping, not {type(value).__name__}"
        )

    if image_spec.key not in images:
        raise ValueError(f"{source}: lock entry {image_spec.key!r} does not exist in the image lock")

    reference = split_image_reference(images[image_spec.key], source)

    # Assemble the chart fields in the chart-convention key order, under the digest spelling.
    fields: dict[str, Yaml] = {"registry": reference.registry, "repository": reference.repository}
    if reference.tag is not None:
        fields["tag"] = reference.tag

    digest = reference.digest
    fields[image_spec.digest_field] = digest.removeprefix("sha256:") if image_spec.digest_field == "sha" else digest

    return fields


def _check_literal_image(node: dict[str, Yaml], source: str) -> None:
    image = node.get(IMAGE_FIELD)
    if isinstance(image, str) and IMAGE_HOLE_PATTERN.search(image) is None:
        raise ValueError(f"{source}: image {image!r} must consume the lock (lock: NAME / lock-image: NAME)")


def split_image_reference(reference: str, source: str) -> ImageReference:
    # Peel the trailing digest.
    path_part, _, digest = reference.rpartition("@")

    # A colon starts a tag only when no slash follows it, so a registry port stays part of the registry.
    tagged = path_part.rpartition(":")
    tag: str | None = None
    registry_part = path_part
    if tagged[1] and "/" not in tagged[2]:
        registry_part, tag = tagged[0], tagged[2]

    # The last slash splits a multi-segment registry from the repository.
    registry, separator, repository = registry_part.rpartition("/")
    if not separator or not registry or not repository:
        raise ValueError(f"{source}: lock reference {reference!r} has no registry/repository split")

    return ImageReference(registry, repository, tag, digest)
