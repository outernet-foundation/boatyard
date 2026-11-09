from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, TypeAdapter

from boatyard.models.config_file import ENV_NAME_PATTERN
from boatyard.temp import TREE_TAG_REGEX
from boatyard.yaml import load_yaml

TREE_TAG_PATTERN = re.compile(rf"^{TREE_TAG_REGEX}$")

PINS_HEADER = "# Deploy record written by 'boatyard bump' — the sole writer; never hand-edit\n"


def _require_sha_slot_pins(pins: dict[str, str]) -> dict[str, str]:
    for name, tag in pins.items():
        if not name.endswith("_SHA") or ENV_NAME_PATTERN.match(name) is None:
            raise ValueError(f"{name!r} must be an ALL_CAPS *_SHA slot name")
        if TREE_TAG_PATTERN.fullmatch(tag) is None:
            raise ValueError(f"{name}: {tag!r} must be a tree-<oid> image tag")
    return pins


PinsMap = Annotated[dict[str, str], AfterValidator(_require_sha_slot_pins)]

_PINS_ADAPTER = TypeAdapter(PinsMap)


def parse_pins(data: object) -> dict[str, str]:
    return _PINS_ADAPTER.validate_python({} if data is None else data)


def load_pins(path: Path, demanded: set[str]) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"{path.as_posix()}: the k8s pins file is required")
    pins = parse_pins(load_yaml(path))
    unpinned = sorted(demanded - pins.keys())
    if unpinned:
        raise ValueError(f"{path.as_posix()}: lacks pins for {', '.join(unpinned)}")
    return pins
