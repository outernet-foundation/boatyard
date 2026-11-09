from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Mapping, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict

from boatyard.models.image import Image, dockerfile_stem, require_dockerfile_spelling
from boatyard.yaml import Yaml, is_list, is_mapping

TEXT_MARKER = "include"
YAML_MARKER = "include-yaml"
LOCK_MARKER = "lock"
LOCK_IMAGE_MARKER = "lock-image"
LOCK_MARKERS = (LOCK_MARKER, LOCK_IMAGE_MARKER)
DOCKERFILE_MARKER = "dockerfile"
IMAGE_FIELD = "image"
IMAGE_SLOT_PATTERN = re.compile(r"\$\{|{{")


class LockImageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    digest_field: Literal["digest", "sha"] = "digest"


class MarkerContext(NamedTuple):
    root: Path
    images: dict[str, str]
    source: str
    ancestors: tuple[Path, ...] = ()
    repository: str | None = None
    builds: Mapping[str, Image] | None = None


class ImageReference(NamedTuple):
    registry: str
    repository: str
    tag: str | None
    digest: str


def compose_image_reference(repository: str, image: Image, tag: str, variant: str | None) -> str:
    fragment = "" if variant is None else f"-{variant}"
    return f"{repository}/{image.name}{fragment}:{tag}"


def env_slot(name: str) -> str:
    return f'{{{{ env "{name}" }}}}'


def resolve_markers(node: Yaml, context: MarkerContext) -> Yaml:
    # Expand includes before locks because spliced content may carry lock markers
    return resolve_locks(expand_includes(node, context), context)


def expand_includes(node: Yaml, context: MarkerContext) -> Yaml:
    match node:
        case list():
            # Resolve every list element
            expanded = [expand_includes(item, context) for item in node]
        case dict():
            match tuple(node.items()):
                # Splice in the included bytes
                case [(marker, value)] if marker == TEXT_MARKER:
                    expanded = include_text(value, context)
                # Splice in the included structure
                case [(marker, value)] if marker == YAML_MARKER:
                    expanded = include_yaml(value, context)
                # Leave lock and dockerfile markers whole for the lock pass to consume or refuse
                case [(marker, _)] if marker in LOCK_MARKERS or marker == DOCKERFILE_MARKER:
                    expanded = node
                # Resolve plain mappings child by child
                case entries:
                    expanded = {key: expand_includes(child, context) for key, child in entries}
        case _:
            # Pass scalars through untouched
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

    # Anchor include paths at the repo root
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{source}: include path {value!r} must anchor at the repo root")

    path = context.root / relative

    # Refuse cycles and missing targets
    if path in context.ancestors:
        raise ValueError(f"{source}: include {value!r} participates in a cycle")
    if not path.is_file():
        raise ValueError(f"{source}: include {value!r} does not exist at the repo root")

    return path


def resolve_locks(node: Yaml, context: MarkerContext) -> Yaml:
    match node:
        case list():
            # Resolve every list element
            resolved = [resolve_locks(item, context) for item in node]
        case dict():
            match tuple(node.items()):
                # Substitute the full locked reference as one string
                case [(marker, value)] if marker == LOCK_MARKER:
                    resolved = locked_reference(value, context)
                # Split the locked reference into chart fields
                case [(marker, value)] if marker == LOCK_IMAGE_MARKER:
                    resolved = lock_image_fields(value, context.images, context.source)
                # Compose the built-image reference from the declared dockerfile
                case [(marker, value)] if marker == DOCKERFILE_MARKER:
                    resolved = dockerfile_reference(value, context)
                # Refuse literal images, then resolve plain mappings child by child
                case entries:
                    _check_literal_image(node, context.source)
                    resolved = {key: resolve_locks(child, context) for key, child in entries}
        case _:
            # Pass scalars through untouched
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


def dockerfile_reference(value: Yaml, context: MarkerContext) -> str:
    match value:
        case str():
            path = value
        case _:
            raise ValueError(
                f"{context.source}: dockerfile takes a repo-root-relative path string, not {type(value).__name__}"
            )

    if context.repository is None or context.builds is None:
        raise ValueError(
            f"{context.source}: dockerfile: is a provisioner-manifest marker; chart values take lock:/lock-image: only"
        )

    image = context.builds.get(dockerfile_stem(path))
    if image is None:
        raise ValueError(f"{context.source}: dockerfile {path!r} names no image in the build set")

    variant = env_slot(image.axis) if image.axis is not None else None
    return compose_image_reference(context.repository, image, env_slot(image.sha_env), variant)


def declared_dockerfile_paths(node: Yaml) -> list[str]:
    if is_list(node):
        return [path for item in node for path in declared_dockerfile_paths(item)]
    if not is_mapping(node):
        return []
    if len(node) == 1 and DOCKERFILE_MARKER in node:
        value = node[DOCKERFILE_MARKER]
        if not isinstance(value, str):
            raise ValueError(f"dockerfile takes a repo-root-relative path string, not {type(value).__name__}")
        require_dockerfile_spelling(value)
        return [value]
    return [path for child in node.values() for path in declared_dockerfile_paths(child)]


def lock_image_fields(value: Yaml, images: dict[str, str], source: str) -> dict[str, Yaml]:
    # Accept a bare lock-entry name or a key/digest_field mapping
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

    # Assemble the chart fields in chart-convention key order under the digest spelling
    fields: dict[str, Yaml] = {"registry": reference.registry, "repository": reference.repository}
    if reference.tag is not None:
        fields["tag"] = reference.tag

    digest = reference.digest
    fields[image_spec.digest_field] = digest.removeprefix("sha256:") if image_spec.digest_field == "sha" else digest

    return fields


def _check_literal_image(node: dict[str, Yaml], source: str) -> None:
    image = node.get(IMAGE_FIELD)
    if isinstance(image, str) and IMAGE_SLOT_PATTERN.search(image) is None:
        raise ValueError(f"{source}: image {image!r} must consume the lock (lock: NAME / lock-image: NAME)")


def split_image_reference(reference: str, source: str) -> ImageReference:
    # Peel the trailing digest
    path_part, _, digest = reference.rpartition("@")

    # Treat a colon as a tag only when no slash follows so registry ports stay whole
    tagged = path_part.rpartition(":")
    tag: str | None = None
    registry_part = path_part
    if tagged[1] and "/" not in tagged[2]:
        registry_part, tag = tagged[0], tagged[2]

    # Split the registry from the repository at the last slash
    registry, separator, repository = registry_part.rpartition("/")
    if not separator or not registry or not repository:
        raise ValueError(f"{source}: lock reference {reference!r} has no registry/repository split")

    return ImageReference(registry, repository, tag, digest)
