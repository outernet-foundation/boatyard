from __future__ import annotations

from collections.abc import Mapping, Set as AbstractSet
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BeforeValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from boatyard.config.config import (
    ENV_NAME_PATTERN,
    PortNumber,
    RESERVED_SLOT_MARKERS,
    Yaml,
    is_mapping,
)
from boatyard.paths import VALUES_FILE
from boatyard.yaml import load_yaml_model

PUBLIC_URL_SLOT = "PUBLIC_URL"
EDGE_BASE_URL_SLOT = "EDGE_BASE_URL"

OVERRIDE_LAYOUT = ("stack", "generated", "compose")


def _coerce_section(values: object) -> dict[str, str]:
    if not is_mapping(values):
        raise ValueError("a section is a NAME: value mapping")
    coerced: dict[str, str] = {}
    for name, value in values.items():
        if isinstance(value, bool):
            coerced[name] = str(value).lower()
        elif isinstance(value, (int, float, str)):
            coerced[name] = str(value)
        else:
            raise TypeError(f"{name}: a section value is a scalar, not {type(value).__name__}")
    return coerced


SectionMap = Annotated[dict[str, str], BeforeValidator(_coerce_section)]


class PublicUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw: str
    forwarded_proto: str
    forwarded_port: str

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: str) -> dict[str, str]:
        scheme, separator, rest = value.partition("://")
        if not separator:
            raise ValueError(f"public_url: {value!r} must carry a scheme (https://example.dev)")
        hostport = rest.partition("/")[0]
        if ":" in hostport:
            forwarded_port = hostport.rpartition(":")[2]
        elif scheme == "https":
            forwarded_port = "443"
        else:
            forwarded_port = "80"
        return {"raw": value, "forwarded_proto": scheme, "forwarded_port": forwarded_port}

    @property
    def forwarded(self) -> dict[str, Yaml]:
        return {"FORWARDED_PROTO": self.forwarded_proto, "FORWARDED_PORT": self.forwarded_port}


class ValuesSections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: dict[str, SectionMap] = Field(default_factory=dict)
    bundles: dict[str, SectionMap] = Field(default_factory=dict)
    shared: SectionMap = Field(default_factory=dict)


class ValuesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundles: list[str] = Field(default_factory=list)
    path_overrides: dict[str, Path] = Field(default_factory=dict)
    public_url: PublicUrl | None = None
    platform: str | None = None
    gateway: PortNumber | None = None
    ports: dict[str, PortNumber] = Field(default_factory=dict)
    values: ValuesSections = Field(default_factory=ValuesSections)
    source: str = Field(default=VALUES_FILE.name, exclude=True)

    @model_validator(mode="after")
    def _refuse_ill_shaped_section_keys(self) -> Self:
        # The per-file key grammar; homes and demands are resolve's to check
        for section_name in ("images", "bundles"):
            entries = self.values.images if section_name == "images" else self.values.bundles
            for name in sorted(entries):
                site = f"values.{section_name}.{name}"
                for key in sorted(entries[name]):
                    self._refuse_key_shape(key, site)
        for name in sorted(self.values.shared):
            self._refuse_key_shape(name, "values.shared")
        return self

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
        values = load_yaml_model(cls, path)
        values.source = path.as_posix()
        return values

    @property
    def flattened(self) -> dict[str, str]:
        values = dict(self.values.shared)
        for section in (*self.values.images.values(), *self.values.bundles.values()):
            values.update(section)
        return values

    def _refuse_key_shape(self, name: str, site: str) -> None:
        if ENV_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"key {name!r} is not an ALL_CAPS environment name")
        if any(marker in name for marker in RESERVED_SLOT_MARKERS):
            raise ValueError(f"key {name!r} must not carry a reserved __BOATYARD_LOCK__/__BOATYARD_TREE__ marker")
        if name == PUBLIC_URL_SLOT:
            raise ValueError(f"{site}: {name} is not hand-settable (minted from public_url)")
        if name == EDGE_BASE_URL_SLOT:
            raise ValueError(f"{site}: {name} is not hand-settable (minted from the gateway declaration)")


def peel_references(references: Mapping[str, AbstractSet[str]]) -> tuple[list[str], list[str]]:
    # Answer the peel order (a name peels only after every name it references peels) and
    # the names a cycle leaves blocked
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
