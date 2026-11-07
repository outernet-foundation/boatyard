from __future__ import annotations

import re
from pathlib import Path
from typing import Self

from pydantic import RootModel, model_validator

from boatyard.config.config import ENV_NAME_PATTERN, Yaml
from boatyard.temp import TREE_TAG_REGEX
from boatyard.yaml import load_yaml_model

TREE_TAG_PATTERN = re.compile(rf"^{TREE_TAG_REGEX}$")

PINS_HEADER = "# Deploy record written by 'boatyard bump' — the sole writer; never hand-edit\n"


class K8sPinsFile(RootModel[dict[str, str]]):
    @model_validator(mode="before")
    @classmethod
    def _coerce_empty(cls, value: Yaml) -> Yaml:
        return {} if value is None else value

    @model_validator(mode="after")
    def _require_sha_slots(self) -> Self:
        for name, tag in self.root.items():
            if not name.endswith("_SHA") or ENV_NAME_PATTERN.match(name) is None:
                raise ValueError(f"{name!r} must be an ALL_CAPS *_SHA slot name")
            if TREE_TAG_PATTERN.fullmatch(tag) is None:
                raise ValueError(f"{name}: {tag!r} must be a tree-<oid> image tag")
        return self

    @classmethod
    def load(cls, path: Path, demanded: set[str]) -> K8sPinsFile:
        if not path.is_file():
            raise ValueError(
                f"{path.as_posix()}: the k8s pins are required; run 'boatyard bump' at a CI-built head "
                "(generation never bootstraps them)"
            )
        pins = load_yaml_model(cls, path)
        unpinned = sorted(demanded - pins.root.keys())
        if unpinned:
            raise ValueError(
                f"{path.as_posix()}: no pin for {', '.join(unpinned)}; run 'boatyard bump' at a CI-built head"
            )
        return pins


def render_pins(service_shas: dict[str, str]) -> str:
    pins = K8sPinsFile.model_validate(service_shas)
    body = "".join(f"{name}: {pins.root[name]}\n" for name in sorted(pins.root)) or "{}\n"
    return PINS_HEADER + body
