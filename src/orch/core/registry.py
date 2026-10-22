"""`agents.yaml` loading, three-level merge, validation and run snapshot (design section 3)."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .predicates import Predicate, parse_when
from .specs.common import PermissionLevel
from .yamlio import load_yaml

CONFIG_VERSION = 1
USER_CONFIG = Path.home() / ".config" / "orch" / "agents.yaml"
PROJECT_CONFIG = Path(".agents") / "agents.yaml"

R = TypeVar("R", bound="_RuleBase")

_RULE_KEYS = frozenset({"id", "enabled", "priority", "when", "prefer", "use"})


class ConfigError(ValueError):
    """A configuration source or the merged result is invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- merged configuration model ---------------------------------------------------------------


class _RuleBase(_Strict):
    id: str = Field(min_length=1)
    enabled: bool = True
    priority: int = 0
    when: dict[str, Any]

    @model_validator(mode="after")
    def _compile_when(self) -> _RuleBase:
        parse_when(self.when)
        return self

    def predicate(self) -> Predicate:
        return parse_when(self.when)


class RouteRule(_RuleBase):
    prefer: list[str] = Field(min_length=1)


class ModelChoice(_Strict):
    model: str = Field(min_length=1)
    effort: str | None = None


class ModelRule(_RuleBase):
    use: ModelChoice


class Defaults(_Strict):
    permissions: PermissionLevel = "full"
    concurrency: int = Field(default=4, ge=1)
    timeout_s: int = Field(default=1800, gt=0)
    fallback: list[str] = []


class RouteLogConfig(_Strict):
    enabled: bool = True
    include_goal: bool = True
    title_max_chars: int = Field(default=120, ge=1)


class Risk(_Strict):
    paths: list[str] = []


class Paths(_Strict):
    worktree_root: str = "auto"


class Routes(_Strict):
    rules: list[RouteRule] = []
    default: Literal["score"] = "score"


class Pricing(_Strict):
    input_per_m: float = Field(ge=0)
    output_per_m: float = Field(ge=0)
    cache_read_per_m: float | None = Field(default=None, ge=0)
    cache_write_per_m: float | None = Field(default=None, ge=0)


class ModelRouting(_Strict):
    rules: list[ModelRule] = []
    default: ModelChoice | None = None
    escalate_on_failure: list[str] | None = None


class Agent(_Strict):
    enabled: bool = True
    cmd: str = Field(min_length=1)
    categories: list[str] = []
    strengths: list[str] = []
    cost: Literal["low", "medium", "high"] | None = None
    concurrency: int | None = Field(default=None, ge=1)
    permissions: PermissionLevel | None = None
    env: dict[str, str] = {}
    pricing: dict[str, Pricing] = {}
    models: dict[str, str] = {}
    model_routing: ModelRouting = ModelRouting()

    def tiers(self) -> list[str]:
        """Escalation order: `escalate_on_failure`, else `models` tier names in declared order."""
        if self.model_routing.escalate_on_failure is not None:
            return list(self.model_routing.escalate_on_failure)
        return list(self.models)


class Config(_Strict):
    version: Literal[1]
    defaults: Defaults = Defaults()
    route_log: RouteLogConfig = RouteLogConfig()
    risk: Risk = Risk()
    paths: Paths = Paths()
    routes: Routes = Routes()
    agents: dict[str, Agent] = {}

    @model_validator(mode="after")
    def _cross_references(self) -> Config:
        problems: list[str] = []
        known = set(self.agents)

        def check_agents(where: str, names: Sequence[str]) -> None:
            problems.extend(f"{where}: unknown agent {n!r}" for n in names if n not in known)

        check_agents("defaults.fallback", self.defaults.fallback)
        for rule in self.routes.rules:
            check_agents(f"routes.rules[{rule.id}].prefer", rule.prefer)
        for name, agent in self.agents.items():
            for tier in agent.model_routing.escalate_on_failure or []:
                if tier not in agent.models:
                    problems.append(
                        f"agents.{name}.model_routing.escalate_on_failure: no tier {tier!r}"
                    )
        if problems:
            raise ValueError("; ".join(problems))
        return self


def effective_rules(rules: Sequence[R]) -> list[R]:
    """Enabled rules by descending `priority`; merged list position breaks ties (section 3)."""
    return sorted((r for r in rules if r.enabled), key=lambda r: -r.priority)


# --- merging ----------------------------------------------------------------------------------


def _is_rule_list(path: tuple[str, ...]) -> bool:
    return path == ("routes", "rules") or (
        len(path) == 4 and path[0] == "agents" and path[2:] == ("model_routing", "rules")
    )


def _check_source(value: Any, label: str, path: tuple[str, ...]) -> None:
    """Reject nulls and check rule ids (required, unique, known keys) in one source."""
    where = ".".join(path) or "<root>"
    if value is None:
        raise ConfigError(f"{label}: {where} is null; null does not delete inherited keys")
    if _is_rule_list(path):
        _check_rule_list(value, label, where)
    if isinstance(value, dict):
        for key, child in cast("dict[Any, Any]", value).items():
            _check_source(child, label, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(cast("list[Any]", value)):
            _check_source(child, label, (*path, str(index)))


def _check_rule_list(rules: Any, label: str, where: str) -> None:
    if not isinstance(rules, list):
        raise ConfigError(f"{label}: {where} must be a list of rules")
    seen: set[str] = set()
    for index, rule in enumerate(cast("list[Any]", rules)):
        if not isinstance(rule, dict):
            raise ConfigError(f"{label}: {where}[{index}] must be a mapping")
        fields = cast("dict[str, Any]", rule)
        rule_id = fields.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ConfigError(f"{label}: {where}[{index}] has no id (anonymous rules are invalid)")
        if rule_id in seen:
            raise ConfigError(f"{label}: {where} has duplicate rule id {rule_id!r}")
        seen.add(rule_id)
        unknown = sorted(set(fields) - _RULE_KEYS)
        if unknown:
            raise ConfigError(f"{label}: {where}[{rule_id}] has unknown keys {unknown}")


def _merge_rules(inherited: Any, override: Any) -> list[dict[str, Any]]:
    merged = [dict(r) for r in cast("list[dict[str, Any]]", inherited)]
    positions = {rule["id"]: i for i, rule in enumerate(merged)}
    for rule in cast("list[dict[str, Any]]", override):
        if rule["id"] in positions:
            # Supplied properties replace inherited ones; `when`, `prefer` and `use` replace whole.
            merged[positions[rule["id"]]].update(copy.deepcopy(rule))
        else:
            positions[rule["id"]] = len(merged)
            merged.append(copy.deepcopy(rule))
    return merged


def _merge(base: dict[str, Any], override: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        here = (*path, key)
        if _is_rule_list(here):
            result[key] = _merge_rules(result.get(key, []), value)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(
                cast("dict[str, Any]", result[key]), cast("dict[str, Any]", value), here
            )
        else:
            result[key] = copy.deepcopy(cast("Any", value))
    return result


def merge_sources(sources: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Merge raw configuration mappings in order; later sources win (section 3)."""
    merged: dict[str, Any] = {}
    for label, data in sources:
        _check_source(data, label, ())
        merged = _merge(merged, data, ())
    return merged


# --- loading ----------------------------------------------------------------------------------


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
    return resources.files("orch.core").joinpath("builtin_agents.yaml").read_text("utf-8")


@dataclass(frozen=True)
class Source:
    label: str
    path: Path | None


@dataclass(frozen=True)
class LoadedConfig:
    config: Config
    sources: tuple[Source, ...]
    snapshot_json: str
    config_hash: str


def validate_merged(merged: dict[str, Any]) -> Config:
    try:
        return Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, "merged configuration")) from exc


def snapshot(config: Config) -> tuple[str, str]:
    """Canonical JSON of the merged configuration and its SHA-256 (`run.started.config_hash`)."""
    text = json.dumps(config.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def config_from_snapshot(text: str) -> Config:
    return validate_merged(json.loads(text))


def load_config(
    project_root: Path | None = None,
    *,
    user_config: Path | None = USER_CONFIG,
    builtin: str | None = None,
) -> LoadedConfig:
    """Built-in defaults, then the user file, then `<project_root>/.agents/agents.yaml`."""
    layers: list[tuple[str, Path | None, str]] = [
        ("built-in defaults", None, builtin if builtin is not None else builtin_text())
    ]
    candidates = [
        ("user config", user_config),
        ("project config", project_root / PROJECT_CONFIG if project_root else None),
    ]
    for label, path in candidates:
        if path is not None and path.is_file():
            layers.append((f"{label} ({path})", path, path.read_text(encoding="utf-8")))
    raw = [(label, read_source(text, label)) for label, _, text in layers]
    config = validate_merged(merge_sources(raw))
    text, digest = snapshot(config)
    return LoadedConfig(
        config, tuple(Source(label, path) for label, path, _ in layers), text, digest
    )
