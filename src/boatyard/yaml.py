from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


def load_yaml(path: Path) -> object:
    # Strict load: refuse duplicate keys, coerce an empty document to the empty mapping
    source = path.as_posix()
    text = path.read_text(encoding="utf-8")
    document_node = yaml.SafeLoader(text).get_single_node()
    if document_node is not None:
        _reject_duplicate_keys(document_node, source)
    loaded = yaml.safe_load(text)
    return {} if loaded is None else loaded


def load_yaml_model[ModelT: BaseModel](
    model: type[ModelT], path: Path, context: dict[str, object] | None = None
) -> ModelT:
    return model.model_validate(load_yaml(path), context=context)


def _reject_duplicate_keys(node: yaml.Node, source: str) -> None:
    if isinstance(node, yaml.MappingNode):
        seen: set[object] = set()
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, yaml.ScalarNode) else repr(key_node.value)
            if key in seen:
                raise ValueError(f"{source}: line {key_node.start_mark.line + 1}: duplicate key {key!r}")
            seen.add(key)
            _reject_duplicate_keys(value_node, source)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _reject_duplicate_keys(item, source)
