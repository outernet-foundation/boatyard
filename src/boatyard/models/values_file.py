from __future__ import annotations

import re
from collections.abc import Mapping, Set as AbstractSet
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from boatyard.models.config_file import (
    ENV_NAME_PATTERN,
    PLACEHOLDER,
    PortNumber,
    RESERVED_SLOT_MARKERS,
)
from boatyard.paths import VALUES_FILE
from boatyard.yaml import is_mapping, load_yaml_model

PUBLIC_HOST_SLOT = "PUBLIC_HOST"
PUBLIC_SCHEME_SLOT = "PUBLIC_SCHEME"
PUBLIC_PORT_SLOT = "PUBLIC_PORT"
PUBLIC_URL_PATH_SLOT = "PUBLIC_URL_PATH"
PUBLIC_URL_SLOT = "PUBLIC_URL"
EDGE_BASE_URL_SLOT = "EDGE_BASE_URL"

PUBLIC_SLOTS = (
    PUBLIC_HOST_SLOT,
    PUBLIC_SCHEME_SLOT,
    PUBLIC_PORT_SLOT,
    PUBLIC_URL_PATH_SLOT,
    PUBLIC_URL_SLOT,
)

OVERRIDE_LAYOUT = ("stack", "generated", "compose")


class ValuesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundles: list[str] = Field(default_factory=list)
    path_overrides: dict[str, Path] = Field(default_factory=dict)
    public_host: str | None = None
    public_scheme: Literal["http", "https"] | None = None
    public_port: PortNumber | None = None
    public_url_path: str | None = None
    platform: str | None = None
    gateway_port: PortNumber | None = None
    ports: dict[str, PortNumber] = Field(default_factory=dict)
    values: ValuesMap = Field(default_factory=dict)
    source: str = Field(default=VALUES_FILE.name, exclude=True)

    @model_validator(mode="after")
    def _validate(self, info: ValidationInfo) -> Self:
        # The per-file grammar: key shape, bare-reference values, and reference acyclicity;
        # orphans and demands are resolve's to check
        source = _source(info)
        for name in sorted(self.values):
            self._refuse_key_shape(name, source)
        self._reference_scan(source)
        hand_references = self._hand_references()
        _order, blocked = _peel_references(hand_references)
        if blocked:
            cycle_nodes = {name: hand_references[name] & set(blocked) for name in blocked}
            # Follow blocked successors until a node repeats
            seen: dict[str, int] = {}
            chain: list[str] = []
            node = min(cycle_nodes)
            while node not in seen:
                seen[node] = len(chain)
                chain.append(node)
                node = min(cycle_nodes[node])
            cycle = chain[seen[node] :]
            raise ValueError(f"{source}: reference cycle: {' -> '.join([*cycle, cycle[0]])}")
        return self

    @field_validator("public_host")
    @classmethod
    def _require_public_host_shape(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip() or "/" in value or ":" in value):
            raise ValueError(f"public_host: {value!r} must be a bare host (no scheme, port, path, or padding)")
        return value

    @field_validator("public_url_path")
    @classmethod
    def _require_public_url_path_shape(cls, value: str | None) -> str | None:
        if value is not None:
            if re.search(r"\s", value) or not value.startswith("/") or value == "/" or value.endswith("/"):
                raise ValueError(
                    f"public_url_path: {value!r} must be a /-rooted path with no whitespace and no trailing / "
                    "(the empty default is the spelling for no path)"
                )
        return value

    @field_validator("path_overrides", mode="before")
    @classmethod
    def _require_path_values(cls, raw_overrides: object) -> object:
        if not is_mapping(raw_overrides):
            return raw_overrides
        path_overrides: dict[str, Path] = {}
        for name, value in raw_overrides.items():
            stripped = str(value).strip()
            if not stripped:
                raise ValueError(f"path_overrides.{name} must be a non-empty filesystem path")
            if "://" in stripped:
                raise ValueError(f"path_overrides.{name} must be a filesystem path, not a URL ({stripped!r})")
            path_overrides[name] = Path(stripped)
        return path_overrides

    @field_validator("path_overrides")
    @classmethod
    def _require_output_layout(cls, path_overrides: dict[str, Path]) -> dict[str, Path]:
        for name, path in path_overrides.items():
            if path.parts[-4:-1] != OVERRIDE_LAYOUT:
                raise ValueError(
                    f"path_overrides.{name}: {path.as_posix()!r} must point into a stack/generated/compose/ bundle output layout"
                )
        return path_overrides

    @classmethod
    def load(cls, path: Path) -> ValuesFile:
        values = load_yaml_model(cls, path, {"path": path.as_posix()})
        values.source = path.as_posix()
        return values

    @property
    def references(self) -> dict[str, set[str]]:
        return self._reference_scan(self.source)

    @property
    def reference_order(self) -> list[str]:
        # The dependency peel over the values; cycles refuse at validation
        order, _blocked = _peel_references(self._hand_references())
        return order

    def _reference_scan(self, source: str) -> dict[str, set[str]]:
        # Collect every value's ${NAME} references, refusing non-bare spellings
        references: dict[str, set[str]] = {}
        for name, value in sorted(self.values.items()):
            if "${" in PLACEHOLDER.sub("", value):
                raise ValueError(f"{source}: {name}: only bare ${{NAME}} references are legal")
            found = PLACEHOLDER.findall(value)
            for reference in found:
                if any(marker in reference for marker in RESERVED_SLOT_MARKERS):
                    raise ValueError(f"{source}: {name}: ${{{reference}}} must not carry a reserved marker")
            references[name] = set(found)
        return references

    def _hand_references(self) -> dict[str, set[str]]:
        hand = set(self.values)
        return {name: found & hand for name, found in self._reference_scan(self.source).items()}

    def _refuse_key_shape(self, name: str, source: str) -> None:
        if ENV_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"{source}: key {name!r} is not an ALL_CAPS environment name")
        if any(marker in name for marker in RESERVED_SLOT_MARKERS):
            raise ValueError(
                f"{source}: key {name!r} must not carry a reserved __BOATYARD_LOCK__/__BOATYARD_TREE__ marker"
            )
        if name in PUBLIC_SLOTS or name == EDGE_BASE_URL_SLOT:
            raise ValueError(f"{source}: {name} is not hand-settable (minted from the public face)")


def _coerce_values(raw: object) -> dict[str, str]:
    if not is_mapping(raw):
        raise ValueError("values: is a NAME: value mapping")
    coerced: dict[str, str] = {}
    for name, value in raw.items():
        if isinstance(value, bool):
            coerced[str(name)] = str(value).lower()
        elif isinstance(value, (int, float, str)):
            coerced[str(name)] = str(value)
        else:
            raise TypeError(f"{name}: a value is a scalar, not {type(value).__name__}")
    return coerced


ValuesMap = Annotated[dict[str, str], BeforeValidator(_coerce_values)]


def _source(info: ValidationInfo) -> str:
    context: dict[str, object] = info.context or {}
    return str(context.get("path", VALUES_FILE.name))


def _peel_references(references: Mapping[str, AbstractSet[str]]) -> tuple[list[str], list[str]]:
    # Answer the peel order and the cycle-blocked names
    blocked: dict[str, set[str]] = {name: set(targets) for name, targets in references.items()}
    order: list[str] = []
    ready = sorted(name for name, targets in blocked.items() if not targets)
    while ready:
        name = ready.pop(0)
        blocked.pop(name)
        order.append(name)
        newly_ready: list[str] = []
        for successor, targets in blocked.items():
            if not targets:
                continue
            targets.discard(name)
            if not targets:
                newly_ready.append(successor)
        ready.extend(sorted(newly_ready))
    return order, sorted(blocked)
