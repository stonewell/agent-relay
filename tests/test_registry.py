from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from relay.core.registry import (
    ConfigError,
    load_config,
    merge_sources,
    read_source,
    validate_merged,
)

BASE = """
version: 1
defaults: { concurrency: 4, timeout_s: 1800, fallback: [a, b] }
agents:
  a:
    cmd: a
    models: { fast: a-1, deep: a-2 }
    categories: [implement, test]
  b: { cmd: b, models: { only: b-1 } }
"""


def src(text: str, label: str = "src") -> tuple[str, dict[str, Any]]:
    return label, read_source(dedent(text), label)


def merged(*texts: str) -> dict[str, Any]:
    return merge_sources([src(t, f"s{i}") for i, t in enumerate(texts)])


def test_later_sources_override_earlier() -> None:
    data = merged(BASE, "defaults: { concurrency: 2 }")
    assert data["defaults"]["concurrency"] == 2
    assert data["defaults"]["timeout_s"] == 1800  # maps merge recursively


def test_primitive_arrays_replace_in_full() -> None:
    data = merged(BASE, "defaults: { fallback: [b] }\nagents: { a: { categories: [review] } }")
    assert data["defaults"]["fallback"] == ["b"]
    assert data["agents"]["a"]["categories"] == ["review"]


def test_false_zero_and_empty_values_are_retained() -> None:
    data = merged(
        BASE, "defaults: { fallback: [] }\nagents: { a: { enabled: false, concurrency: 0 } }"
    )
    assert data["defaults"]["fallback"] == []
    assert data["agents"]["a"]["enabled"] is False
    assert data["agents"]["a"]["concurrency"] == 0
    with pytest.raises(ConfigError, match="concurrency"):
        validate_merged(data)  # 0 is retained, then rejected by its own schema, not dropped


def test_sources_are_not_mutated_by_merging() -> None:
    first = src(BASE, "first")
    second = src("agents: { a: { categories: [review] } }", "second")
    merge_sources([first, second])
    assert first[1]["agents"]["a"]["categories"] == ["implement", "test"]


def test_null_error_names_the_source_and_path() -> None:
    with pytest.raises(ConfigError, match=r"s1: defaults.concurrency is null"):
        merged(BASE, "defaults: { concurrency: null }")


def test_duplicate_yaml_keys_are_rejected() -> None:
    with pytest.raises(ConfigError, match="duplicate key"):
        read_source("defaults: { concurrency: 1, concurrency: 2 }", "dup")


def test_invalid_yaml_and_non_mapping_are_rejected() -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        read_source("a: [unclosed", "bad")
    with pytest.raises(ConfigError, match="mapping"):
        read_source("- a\n- b", "list")


def test_missing_user_file_is_fine(tmp_path: Path) -> None:
    loaded = load_config(user_config=tmp_path / "nope.yaml")
    assert len(loaded.sources) == 1


def test_error_in_a_file_names_that_file(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text("defaults: { concurrency: null }\n")
    with pytest.raises(ConfigError, match=r"user config .*agents\.yaml.*null"):
        load_config(user_config=path)


def test_user_file_without_version_inherits_builtin_version(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text("defaults: { concurrency: 1 }\n")
    assert load_config(user_config=path).config.defaults.concurrency == 1


def test_builtin_defaults_validate() -> None:
    cfg = load_config(user_config=None).config
    assert set(cfg.agents) == {"claude", "codex", "cursor"}
    assert cfg.agents["cursor"].enabled is False
    assert cfg.defaults.fallback == ["codex", "claude"]


@pytest.mark.parametrize("section", ["tui", "routes", "coordination", "paths", "route_log"])
def test_removed_configuration_is_rejected(section: str) -> None:
    with pytest.raises(ConfigError, match=section):
        validate_merged(merged(BASE, f"{section}: {{}}"))
