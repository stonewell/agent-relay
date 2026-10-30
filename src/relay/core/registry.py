"""Load and validate completion backend configuration."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .yamlio import load_yaml

CONFIG_VERSION = 1
USER_CONFIG = Path.home() / ".config" / "relay" / "agents.yaml"


class ConfigError(ValueError):
    """A configuration source or the merged result is invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Defaults(_Strict):
    concurrency: int = Field(default=4, ge=1)
    timeout_s: int = Field(default=1800, gt=0)
    fallback: list[str] = []


class Agent(_Strict):
    enabled: bool = True
    cmd: str = Field(min_length=1)
    categories: list[str] = []
    cost: Literal["low", "medium", "high"] | None = None
    concurrency: int | None = Field(default=None, ge=1)
    env: dict[str, str] = {}
    models: dict[str, str] = {}


class Config(_Strict):
    version: Literal[1]
    defaults: Defaults = Defaults()
    agents: dict[str, Agent] = {}

    @model_validator(mode="after")
    def _cross_references(self) -> Config:
        unknown = [name for name in self.defaults.fallback if name not in self.agents]
        if unknown:
            raise ValueError(f"defaults.fallback: unknown agents {unknown}")
        return self


def _check_source(value: Any, label: str, path: tuple[str, ...]) -> None:
    if value is None:
        where = ".".join(path) or "<root>"
        raise ConfigError(f"{label}: {where} is null; null does not delete inherited keys")
    if isinstance(value, dict):
        for key, child in cast("dict[Any, Any]", value).items():
            _check_source(child, label, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(cast("list[Any]", value)):
            _check_source(child, label, (*path, str(index)))


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(cast("dict[str, Any]", result[key]), cast("dict[str, Any]", value))
        else:
            result[key] = copy.deepcopy(cast("Any", value))
    return result


def merge_sources(sources: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Merge configuration mappings in order; later sources win."""
    merged: dict[str, Any] = {}
    for label, data in sources:
        _check_source(data, label, ())
        merged = _merge(merged, data)
    return merged


def _format_errors(error: ValidationError, label: str) -> str:
    lines = [
        f"{label}: {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
        for e in error.errors()
    ]
    return "\n".join(lines)


def read_source(text: str, label: str) -> dict[str, Any]:
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{label}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{label}: top level must be a mapping")
    return data  # pyright: ignore[reportUnknownVariableType]


def builtin_text() -> str:
    return resources.files("relay.core").joinpath("builtin_agents.yaml").read_text("utf-8")


@dataclass(frozen=True)
class Source:
    label: str
    path: Path | None


@dataclass(frozen=True)
class LoadedConfig:
    config: Config
    sources: tuple[Source, ...]


def validate_merged(merged: dict[str, Any]) -> Config:
    try:
        return Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, "merged configuration")) from exc


def load_config(
    *,
    user_config: Path | None = USER_CONFIG,
    builtin: str | None = None,
) -> LoadedConfig:
    """Built-in defaults, then the user configuration file."""
    layers: list[tuple[str, Path | None, str]] = [
        ("built-in defaults", None, builtin if builtin is not None else builtin_text())
    ]
    if user_config is not None and user_config.is_file():
        layers.append(
            (f"user config ({user_config})", user_config, user_config.read_text(encoding="utf-8"))
        )
    raw = [(label, read_source(text, label)) for label, _, text in layers]
    config = validate_merged(merge_sources(raw))
    return LoadedConfig(config, tuple(Source(label, path) for label, path, _ in layers))
