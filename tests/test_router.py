"""Agent router behaviour (design section 4.2)."""

from __future__ import annotations

from typing import Any, cast

from relay.core.classifier import Classification, RiskLevel
from relay.core.registry import Config, validate_merged
from relay.core.router import Excluded, route_agent
from relay.core.specs.common import Category, Complexity
from relay.core.specs.route import Alternative


def cls(
    category: Category = "implement",
    complexity: Complexity = "low",
    risk: RiskLevel = "normal",
    language: str | None = None,
) -> Classification:
    return Classification(category=category, complexity=complexity, risk=risk, language=language)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        child = result.get(key)
        if isinstance(value, dict) and isinstance(child, dict):
            result[key] = _deep_merge(cast("dict[str, Any]", child), cast("dict[str, Any]", value))
        else:
            result[key] = value
    return result


def config(**overrides: Any) -> Config:
    data: dict[str, Any] = {
        "version": 1,
        "defaults": {"fallback": ["a", "b"], "concurrency": 2, "permissions": "full"},
        "agents": {
            "a": {
                "cmd": "a",
                "categories": ["implement"],
                "models": {"fast": "a-1", "deep": "a-2"},
                "cost": "low",
            },
            "b": {
                "cmd": "b",
                "categories": ["review"],
                "models": {"only": "b-1"},
                "cost": "high",
            },
            "c": {"cmd": "c", "categories": ["implement"], "models": {}, "cost": "medium"},
        },
    }
    return validate_merged(_deep_merge(data, overrides))


# --- pinned agent -----------------------------------------------------------------------------


def test_pinned_agent_is_used() -> None:
    decision = route_agent(config(), cls(), pinned="a")
    assert decision.agent == "a"
    assert decision.agent_rule == "pinned"
    assert not decision.queued and not decision.rejected


def test_pinned_agent_queues_when_busy() -> None:
    decision = route_agent(config(), cls(), pinned="a", load={"a": 2})
    assert decision.agent == "a"
    assert decision.queued
    assert not decision.rejected


def test_pinned_agent_unavailable_fails_and_is_not_replaced() -> None:
    decision = route_agent(config(), cls(), pinned="a", available={"a": False})
    assert decision.rejected
    assert decision.agent is None
    assert decision.exhausted == (Excluded("a", "unavailable"),)


def test_pinned_agent_without_models_fails() -> None:
    decision = route_agent(config(), cls(), pinned="c")
    assert decision.rejected
    assert decision.exhausted == (Excluded("c", "no models"),)


def test_pinned_agent_unsupported_permission_fails_closed() -> None:
    decision = route_agent(config(), cls(), pinned="a", permissions="workspace")
    assert decision.rejected
    assert decision.exhausted == (Excluded("a", "permission workspace unsupported"),)


def test_pinned_agent_respects_levels() -> None:
    decision = route_agent(
        config(), cls(), pinned="a", permissions="readonly", levels={"a": ("readonly",)}
    )
    assert decision.agent == "a"
    assert not decision.rejected


def test_pinned_agent_missing_from_probe_maps_is_available() -> None:
    # `a` is absent from both `available` and `levels`: the defaults treat it as available
    # and able to enforce `full` (the requested level).
    decision = route_agent(config(), cls(), pinned="a", available={}, levels={})
    assert decision.agent == "a"
    assert not decision.rejected and not decision.queued


def test_unknown_pinned_agent_is_rejected() -> None:
    decision = route_agent(config(), cls(), pinned="ghost")
    assert decision.rejected
    assert "unknown agent" in decision.reason


# --- routing rules ----------------------------------------------------------------------------


def test_first_matching_rule_in_priority_order_wins() -> None:
    cfg = config(
        routes={
            "rules": [
                {
                    "id": "r-high",
                    "when": {"category": ["implement"]},
                    "prefer": ["b"],
                    "priority": 10,
                },
                {"id": "r-low", "when": {"category": ["implement"]}, "prefer": ["a"]},
            ]
        }
    )
    decision = route_agent(cfg, cls())
    assert decision.agent == "b"
    assert decision.agent_rule == "r-high"


def test_rule_must_match_all_attributes() -> None:
    cfg = config(
        routes={
            "rules": [
                {
                    "id": "r-py",
                    "when": {"category": ["implement"], "language": ["python"]},
                    "prefer": ["b"],
                },
                {"id": "r-any", "when": {"category": ["implement"]}, "prefer": ["a"]},
            ]
        }
    )
    assert route_agent(cfg, cls(language="typescript")).agent == "a"
    assert route_agent(cfg, cls(language="python")).agent == "b"


def test_rule_filters_unavailable_candidates() -> None:
    cfg = config(
        routes={"rules": [{"id": "r", "when": {"category": ["implement"]}, "prefer": ["c", "a"]}]}
    )
    decision = route_agent(cfg, cls(), available={"c": False})
    assert decision.agent == "a"
    assert decision.agent_rule == "r"
    assert decision.exhausted == (Excluded("c", "unavailable"),)


def test_rule_filters_disabled_candidates() -> None:
    cfg = config(
        routes={"rules": [{"id": "r", "when": {"category": ["implement"]}, "prefer": ["c", "a"]}]},
        agents={"c": {"enabled": False}},
    )
    decision = route_agent(cfg, cls())
    assert decision.agent == "a"
    assert decision.exhausted == (Excluded("c", "disabled"),)


def test_rule_picks_first_eligible_with_capacity() -> None:
    cfg = config(
        routes={"rules": [{"id": "r", "when": {"category": ["implement"]}, "prefer": ["a", "b"]}]}
    )
    decision = route_agent(cfg, cls(), load={"a": 2})
    assert decision.agent == "b"
    assert not decision.queued
    assert decision.busy == ("a",)


def test_rule_queues_when_all_eligible_are_busy() -> None:
    cfg = config(
        routes={"rules": [{"id": "r", "when": {"category": ["implement"]}, "prefer": ["a", "b"]}]}
    )
    decision = route_agent(cfg, cls(), load={"a": 2, "b": 2})
    assert decision.agent == "a"
    assert decision.queued
    assert not decision.rejected
    assert decision.busy == ("a", "b")


def test_mixed_prefer_list_queues_busy_and_records_exhausted() -> None:
    cfg = config(
        routes={"rules": [{"id": "r", "when": {"category": ["implement"]}, "prefer": ["a", "c"]}]}
    )
    # `a` is eligible but busy; `c` has no models. The busy agent queues and the ineligible
    # one is recorded as exhausted.
    decision = route_agent(cfg, cls(), load={"a": 2})
    assert decision.agent == "a"
    assert decision.queued
    assert not decision.rejected
    assert decision.busy == ("a",)
    assert decision.exhausted == (Excluded("c", "no models"),)


def test_exhausted_preference_list_falls_back_to_score() -> None:
    cfg = config(
        routes={"rules": [{"id": "r", "when": {"category": ["implement"]}, "prefer": ["c"]}]}
    )
    # c has no models, so the preferred list is exhausted; scoring picks `a` (category match).
    decision = route_agent(cfg, cls())
    assert decision.agent == "a"
    assert decision.agent_rule == "category-match"
    assert decision.exhausted == (Excluded("c", "no models"),)
    assert "rule r preferred list exhausted" in decision.reason


def test_rule_predicate_receives_attempt_and_numeric_inputs() -> None:
    cfg = config(
        routes={
            "rules": [
                {
                    "id": "r-retry",
                    "when": {"category": ["implement"], "attempt": ">=2", "files": ">=3"},
                    "prefer": ["b"],
                },
                {"id": "r-first", "when": {"category": ["implement"]}, "prefer": ["a"]},
            ]
        }
    )
    assert route_agent(cfg, cls()).agent == "a"
    assert route_agent(cfg, cls(), attempt=2, files=5).agent == "b"


# --- score fallback ---------------------------------------------------------------------------


def test_score_prefers_category_match() -> None:
    # category "review": only `b` lists it.
    decision = route_agent(config(), cls(category="review"))
    assert decision.agent == "b"
    assert decision.agent_rule == "category-match"


def test_score_tie_breaks_by_fallback_order() -> None:
    cfg = config(
        agents={"b": {"categories": ["implement"], "cost": "low"}},
        defaults={"fallback": ["b", "a"]},
    )
    decision = route_agent(cfg, cls())
    assert decision.agent == "b"
    assert decision.agent_rule == "score"  # two category matches -> score, not category-match


def test_cost_penalty_breaks_ties() -> None:
    cfg = config(
        agents={
            "a": {"cost": "high"},
            "b": {"categories": ["implement"], "cost": "low"},
        },
        defaults={"fallback": ["a", "b"]},
    )
    # a is earlier in fallback order but its high cost makes b win.
    assert route_agent(cfg, cls()).agent == "b"


def test_score_queues_when_all_busy() -> None:
    decision = route_agent(config(), cls(), load={"a": 2, "b": 2})
    assert decision.queued
    assert decision.agent == "a"
    assert decision.busy == ("a", "b")


def test_score_alternatives_carry_scores() -> None:
    decision = route_agent(config(), cls(category="implement"))
    assert decision.agent == "a"
    assert decision.agent_rule == "category-match"
    assert decision.alternatives == (Alternative(agent="b", score=-0.2),)


def test_score_no_eligible_agent_is_rejected() -> None:
    decision = route_agent(config(), cls(), available={"a": False, "b": False, "c": False})
    assert decision.rejected
    assert decision.agent is None
    assert decision.agent_rule == "score"


def test_recent_failure_penalty_changes_winner() -> None:
    cfg = config(
        agents={"b": {"categories": ["implement"], "cost": "low"}},
        defaults={"fallback": ["a", "b"]},
    )
    assert route_agent(cfg, cls()).agent == "a"
    assert route_agent(cfg, cls(), recent_failures={"a": 3}).agent == "b"


def test_routing_is_deterministic() -> None:
    cfg = config(
        routes={"rules": [{"id": "r", "when": {"category": ["implement"]}, "prefer": ["a", "b"]}]}
    )
    first = route_agent(cfg, cls(), load={"a": 2})
    second = route_agent(cfg, cls(), load={"a": 2})
    assert first == second
