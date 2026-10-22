from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from design_blocks import blocks_in
from orch.core.registry import (
    Config,
    ConfigError,
    config_from_snapshot,
    effective_rules,
    load_config,
    merge_sources,
    read_source,
    validate_merged,
)

BASE = """
version: 1
defaults: { concurrency: 4, fallback: [a, b], permissions: full }
routes:
  rules:
    - { id: r-impl, when: { category: [implement] }, prefer: [a, b] }
    - { id: r-review, when: { category: [review] }, prefer: [b] }
    - { id: r-test, when: { category: [test] }, prefer: [a] }
agents:
  a:
    cmd: a
    models: { fast: a-1, deep: a-2 }
    categories: [implement, test]
    model_routing:
      rules:
        - { id: m-hard, when: { complexity: ">=high" }, use: { model: deep } }
      default: { model: fast }
  b: { cmd: b, models: { only: b-1 } }
"""


def src(text: str, label: str = "src") -> tuple[str, dict[str, Any]]:
    return label, read_source(dedent(text), label)


def merged(*texts: str) -> dict[str, Any]:
    return merge_sources([src(t, f"s{i}") for i, t in enumerate(texts)])


def ids(rules: list[dict[str, Any]]) -> list[str]:
    return [r["id"] for r in rules]


# --- merge order and semantics ----------------------------------------------------------------


def test_later_sources_override_earlier() -> None:
    data = merged(BASE, "defaults: { concurrency: 2 }")
    assert data["defaults"]["concurrency"] == 2
    assert data["defaults"]["permissions"] == "full"  # maps merge recursively


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


def test_keyed_rules_override_in_place_and_append_new_ids() -> None:
    data = merged(
        BASE,
        """
        routes:
          rules:
            - { id: r-review, prefer: [a] }
            - { id: r-new, when: { category: [debug] }, prefer: [a] }
        """,
    )
    rules = data["routes"]["rules"]
    assert ids(rules) == ["r-impl", "r-review", "r-test", "r-new"]
    assert rules[1]["prefer"] == ["a"]
    assert rules[1]["when"] == {"category": ["review"]}  # untouched property inherited


def test_when_and_action_blocks_replace_in_full() -> None:
    data = merged(
        BASE,
        "routes: { rules: [ { id: r-impl, when: { category: [test] }, prefer: [b] } ] }",
    )
    rule = data["routes"]["rules"][0]
    assert rule["when"] == {"category": ["test"]}  # not merged with [implement]
    assert rule["prefer"] == ["b"]


def test_empty_rule_list_adds_no_overrides() -> None:
    data = merged(BASE, "routes: { rules: [] }")
    assert ids(data["routes"]["rules"]) == ["r-impl", "r-review", "r-test"]


def test_model_rules_merge_per_agent_independently() -> None:
    data = merged(
        BASE,
        """
        agents:
          a:
            model_routing:
              rules: [ { id: m-hard, enabled: false } ]
        """,
    )
    assert data["agents"]["a"]["model_routing"]["rules"][0]["enabled"] is False
    assert data["agents"]["a"]["model_routing"]["default"] == {"model": "fast"}
    assert "model_routing" not in data["agents"]["b"]


def test_rules_inside_a_new_agent_are_checked_and_kept() -> None:
    data = merged(
        BASE,
        "agents: { c: { cmd: c, model_routing: { rules: [ { id: x, when: {}, use: { model: m } } ] } } }",
    )
    assert ids(data["agents"]["c"]["model_routing"]["rules"]) == ["x"]


def test_sources_are_not_mutated_by_merging() -> None:
    first = src(BASE, "first")
    second = src("routes: { rules: [ { id: r-impl, prefer: [b] } ] }", "second")
    merge_sources([first, second])
    assert first[1]["routes"]["rules"][0]["prefer"] == ["a", "b"]


# --- invalid input ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("defaults: { concurrency: null }", "null"),
        ("agents: { a: { cmd: null } }", "null"),
        ("routes: { rules: [ { when: {}, prefer: [a] } ] }", "anonymous"),
        ("routes: { rules: [ { id: '', when: {}, prefer: [a] } ] }", "anonymous"),
        (
            "routes: { rules: [ { id: x, when: {}, prefer: [a] }, { id: x, when: {}, prefer: [b] } ] }",
            "duplicate rule id",
        ),
        ("routes: { rules: [ { id: x, bogus: 1 } ] }", "unknown keys"),
        ("routes: { rules: { id: x } }", "list of rules"),
        ("routes: { rules: [ plain-string ] }", "mapping"),
        (
            "agents: { a: { model_routing: { rules: [ { when: {}, use: { model: m } } ] } } }",
            "anonymous",
        ),
    ],
)
def test_invalid_source_is_rejected(text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        merged(BASE, text)


def test_null_error_names_the_source_and_path() -> None:
    with pytest.raises(ConfigError, match=r"s1: defaults.concurrency is null"):
        merged(BASE, "defaults: { concurrency: null }")


def test_duplicate_ids_are_checked_per_source_not_across_sources() -> None:
    data = merged(BASE, "routes: { rules: [ { id: r-impl, prefer: [b] } ] }")
    assert ids(data["routes"]["rules"]).count("r-impl") == 1


def test_duplicate_yaml_keys_are_rejected() -> None:
    with pytest.raises(ConfigError, match="duplicate key"):
        read_source("defaults: { concurrency: 1, concurrency: 2 }", "dup")


def test_invalid_yaml_and_non_mapping_are_rejected() -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        read_source("a: [unclosed", "bad")
    with pytest.raises(ConfigError, match="mapping"):
        read_source("- a\n- b", "list")


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("colour: red", "colour"),
        ("version: 2", "version"),
        ("agents: { a: { cmd: a, bogus: 1 } }", "bogus"),
        ("routes: { default: llm }", "default"),
        ("defaults: { fallback: [ghost] }", "unknown agent 'ghost'"),
        ("routes: { rules: [ { id: x, when: {}, prefer: [ghost] } ] }", "unknown agent 'ghost'"),
        ("routes: { rules: [ { id: x, when: { category: [poetry] }, prefer: [a] } ] }", "category"),
        ("routes: { rules: [ { id: x, when: { cost_ceiling: '<5' }, prefer: [a] } ] }", "phase 4"),
        ("routes: { rules: [ { id: x, prefer: [a] } ] }", "when"),
        ("routes: { rules: [ { id: x, when: {} } ] }", "prefer"),
        ("routes: { rules: [ { id: x, when: {}, prefer: [] } ] }", "prefer"),
        (
            "agents: { a: { model_routing: { escalate_on_failure: [fast, nope] } } }",
            "no tier 'nope'",
        ),
        ("agents: { a: { cost: cheap } }", "cost"),
        ("defaults: { permissions: root }", "permissions"),
        ("agents: { a: { pricing: { m: { input_per_m: -1, output_per_m: 1 } } } }", "input_per_m"),
    ],
)
def test_merged_config_validation(patch: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        validate_merged(merged(BASE, patch))


def test_model_rule_missing_use_is_rejected() -> None:
    patch = "agents: { a: { model_routing: { rules: [ { id: x, when: {} } ] } } }"
    with pytest.raises(ConfigError, match="use"):
        validate_merged(merged(BASE, patch))


# --- effective rule order ---------------------------------------------------------------------


def config(*texts: str) -> Config:
    return validate_merged(merged(BASE, *texts))


def test_disabled_rules_are_not_effective() -> None:
    cfg = config("routes: { rules: [ { id: r-review, enabled: false } ] }")
    assert [r.id for r in effective_rules(cfg.routes.rules)] == ["r-impl", "r-test"]


def test_priority_orders_descending_with_stable_ties() -> None:
    cfg = config(
        """
        routes:
          rules:
            - { id: r-test, priority: 5 }
            - { id: r-review, priority: 5 }
            - { id: r-late, priority: 5, when: {}, prefer: [a] }
        """
    )
    # Merged positions are r-impl, r-review, r-test, r-late; equal priority keeps that order.
    assert [r.id for r in effective_rules(cfg.routes.rules)] == [
        "r-review",
        "r-test",
        "r-late",
        "r-impl",
    ]


def test_project_exception_can_precede_general_inherited_rule() -> None:
    cfg = config(
        "routes: { rules: [ { id: proj-impl, priority: 10, when: { category: [implement] }, prefer: [b] } ] }"
    )
    first = effective_rules(cfg.routes.rules)[0]
    assert first.id == "proj-impl"
    assert first.predicate().matches({"category": "implement"})


def test_rule_predicates_evaluate_against_facts() -> None:
    cfg = config()
    hard = cfg.agents["a"].model_routing.rules[0]
    assert hard.predicate().matches({"complexity": "high"})
    assert not hard.predicate().matches({"complexity": "medium"})


# --- escalation tiers -------------------------------------------------------------------------


def test_default_escalation_order_is_models_in_declaration_order() -> None:
    cfg = config()
    assert cfg.agents["a"].tiers() == ["fast", "deep"]
    assert cfg.agents["b"].tiers() == ["only"]  # a single tier never escalates


def test_explicit_escalation_list_wins() -> None:
    cfg = config("agents: { a: { model_routing: { escalate_on_failure: [deep] } } }")
    assert cfg.agents["a"].tiers() == ["deep"]


# --- built-in defaults, three levels, snapshot ------------------------------------------------


def test_builtin_defaults_validate() -> None:
    loaded = load_config(None, user_config=None)
    cfg = loaded.config
    assert set(cfg.agents) == {"claude", "codex", "gemini", "cursor"}
    assert cfg.agents["cursor"].enabled is False
    assert cfg.agents["claude"].tiers() == ["fast", "balanced", "deep"]
    assert cfg.agents["codex"].models["deep"] == "default"
    assert [r.id for r in effective_rules(cfg.routes.rules)][0] == "route-bugfix-codex"
    assert all(a.pricing == {} for a in cfg.agents.values())


def test_design_section_3_example_validates() -> None:
    (block,) = blocks_in("3.", "yaml")
    cfg = validate_merged(read_source(block.text, "design"))
    assert cfg.agents["claude"].model_routing.rules[2].id == "claude-cheap"


def test_three_level_merge_order(tmp_path: Path) -> None:
    user = tmp_path / "user.yaml"
    user.write_text(
        "version: 1\ndefaults: { concurrency: 8, timeout_s: 100 }\n"
        "routes: { rules: [ { id: route-review-claude, prefer: [codex] } ] }\n",
        encoding="utf-8",
    )
    project = tmp_path / "repo"
    (project / ".agents").mkdir(parents=True)
    (project / ".agents" / "agents.yaml").write_text(
        "version: 1\ndefaults: { concurrency: 2 }\n"
        "routes: { rules: [ { id: route-review-claude, enabled: false } ] }\n",
        encoding="utf-8",
    )
    loaded = load_config(project, user_config=user)
    cfg = loaded.config
    assert cfg.defaults.concurrency == 2  # project beats user
    assert cfg.defaults.timeout_s == 100  # user beats built-in
    assert cfg.defaults.permissions == "full"  # built-in survives
    review = next(r for r in cfg.routes.rules if r.id == "route-review-claude")
    assert review.prefer == ["codex"]  # user override of the built-in
    assert review.enabled is False  # project disabled it
    assert [s.label.split(" ")[0] for s in loaded.sources] == ["built-in", "user", "project"]


def test_missing_user_and_project_files_are_fine(tmp_path: Path) -> None:
    loaded = load_config(tmp_path, user_config=tmp_path / "nope.yaml")
    assert len(loaded.sources) == 1


def test_error_in_a_file_names_that_file(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".agents").mkdir()
    (project / ".agents" / "agents.yaml").write_text("defaults: { concurrency: null }\n")
    with pytest.raises(ConfigError, match=r"project config .*agents\.yaml.*null"):
        load_config(project, user_config=None)


def test_project_file_without_version_inherits_builtin_version(tmp_path: Path) -> None:
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text("defaults: { concurrency: 1 }\n")
    assert load_config(tmp_path, user_config=None).config.defaults.concurrency == 1


def test_snapshot_round_trips_and_hash_is_stable(tmp_path: Path) -> None:
    first = load_config(None, user_config=None)
    second = load_config(None, user_config=None)
    assert first.config_hash == second.config_hash
    restored = config_from_snapshot(first.snapshot_json)
    assert restored == first.config
    assert restored.agents["claude"].tiers() == ["fast", "balanced", "deep"]  # order survives


def test_hash_changes_when_configuration_changes(tmp_path: Path) -> None:
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text("defaults: { concurrency: 1 }\n")
    assert (
        load_config(tmp_path, user_config=None).config_hash
        != load_config(None, user_config=None).config_hash
    )
