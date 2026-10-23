"""Model router behaviour (design section 4.3)."""

from __future__ import annotations

from typing import Any

from relay.core.classifier import Classification, RiskLevel
from relay.core.model_router import (
    escalate,
    escalation_tiers,
    next_escalation_tier,
    select_model,
)
from relay.core.registry import Agent
from relay.core.specs.common import Category, Complexity


def cls(
    category: Category = "implement",
    complexity: Complexity = "low",
    risk: RiskLevel = "normal",
    language: str | None = None,
) -> Classification:
    return Classification(category=category, complexity=complexity, risk=risk, language=language)


def agent(
    models: dict[str, str] | None = None,
    rules: list[dict[str, Any]] | None = None,
    default: dict[str, Any] | None = None,
    escalate_on_failure: list[str] | None = None,
) -> Agent:
    routing: dict[str, Any] = {}
    if rules is not None:
        routing["rules"] = rules
    if default is not None:
        routing["default"] = default
    if escalate_on_failure is not None:
        routing["escalate_on_failure"] = escalate_on_failure
    return Agent.model_validate({"cmd": "x", "models": models or {}, "model_routing": routing})


def claude_like() -> Agent:
    return agent(
        models={
            "fast": "claude-haiku-5-5",
            "balanced": "claude-sonnet-5-5",
            "deep": "claude-opus-5-5",
        },
        rules=[
            {
                "id": "claude-deep-review",
                "when": {"category": ["review"], "complexity": ">=high"},
                "use": {"model": "deep"},
            },
            {
                "id": "claude-impl",
                "when": {"category": ["implement", "refactor", "bugfix"]},
                "use": {"model": "balanced"},
            },
            {
                "id": "claude-cheap",
                "when": {"category": ["docs-for-code"], "est_tokens": "<5000"},
                "use": {"model": "fast"},
            },
        ],
        default={"model": "balanced"},
        escalate_on_failure=["fast", "balanced", "deep"],
    )


# --- rule-based selection ---------------------------------------------------------------------


def test_first_matching_rule_wins() -> None:
    decision = select_model(claude_like(), cls(category="review", complexity="high"))
    assert decision.model == "claude-opus-5-5"
    assert decision.tier == "deep"
    assert decision.model_rule == "claude-deep-review"


def test_default_when_no_rule_matches() -> None:
    decision = select_model(claude_like(), cls(category="debug"))
    assert decision.model == "claude-sonnet-5-5"
    assert decision.tier == "balanced"
    assert decision.model_rule == "default"


def test_effort_is_recorded() -> None:
    codex = agent(
        models={"fast": "default", "balanced": "default", "deep": "default"},
        rules=[
            {
                "id": "codex-fast-tests",
                "when": {"category": ["test"], "complexity": "<=medium"},
                "use": {"model": "fast", "effort": "low"},
            },
        ],
        default={"model": "balanced", "effort": "medium"},
    )
    assert select_model(codex, cls(category="test", complexity="low")).effort == "low"
    assert select_model(codex, cls(category="test", complexity="high")).effort == "medium"


def test_literal_model_id_is_not_a_tier() -> None:
    a = agent(models={"balanced": "real-model"}, default={"model": "custom-literal"})
    decision = select_model(a, cls())
    assert decision.model == "custom-literal"
    assert decision.tier is None


def test_numeric_and_language_inputs() -> None:
    a = agent(
        models={"fast": "f", "balanced": "b"},
        rules=[
            {
                "id": "r",
                "when": {"est_tokens": "<5000", "files": ">=2", "language": ["python"]},
                "use": {"model": "fast"},
            }
        ],
        default={"model": "balanced"},
    )
    assert select_model(a, cls(language="python"), est_tokens=1000, files=2).model == "f"
    assert select_model(a, cls(language="python"), est_tokens=9000, files=2).model == "b"
    assert select_model(a, cls(language="python"), est_tokens=1000, files=1).model == "b"
    assert select_model(a, cls(language="rust"), est_tokens=1000, files=2).model == "b"


def test_attempt_is_a_rule_input() -> None:
    a = agent(
        models={"fast": "f", "deep": "d"},
        rules=[
            {"id": "retry", "when": {"attempt": ">=2"}, "use": {"model": "deep"}},
        ],
        default={"model": "fast"},
    )
    assert select_model(a, cls(), attempt=1).model == "f"
    assert select_model(a, cls(), attempt=2).model == "d"


def test_each_agents_rules_are_independent() -> None:
    claude = claude_like()
    gemini = agent(models={"fast": "g-fast", "deep": "g-deep"}, default={"model": "fast"})
    assert (
        select_model(claude, cls(category="review", complexity="high")).model == "claude-opus-5-5"
    )
    # gemini has no rules: the same task gets its own default.
    assert select_model(gemini, cls(category="review", complexity="high")).model == "g-fast"


def test_no_default_falls_back_to_first_tier() -> None:
    a = agent(models={"fast": "f", "deep": "d"})
    decision = select_model(a, cls())
    assert decision.model == "f"
    assert decision.tier == "fast"
    assert decision.model_rule == "default"


# --- escalation -------------------------------------------------------------------------------


def test_escalation_default_is_models_declaration_order() -> None:
    a = agent(models={"fast": "f", "balanced": "b", "deep": "d"}, default={"model": "fast"})
    assert escalation_tiers(a) == ("fast", "balanced", "deep")


def test_explicit_escalation_list_wins() -> None:
    a = agent(
        models={"fast": "f", "balanced": "b", "deep": "d"},
        default={"model": "fast"},
        escalate_on_failure=["fast", "deep"],
    )
    assert escalation_tiers(a) == ("fast", "deep")


def test_single_tier_never_escalates() -> None:
    a = agent(models={"only": "o"}, default={"model": "only"})
    decision = select_model(a, cls())
    assert next_escalation_tier(a, decision.tier) is None
    assert escalate(a, decision) == decision


def test_top_tier_ceiling() -> None:
    a = claude_like()
    top = select_model(a, cls(category="review", complexity="high"))  # deep
    assert top.tier == "deep"
    assert next_escalation_tier(a, top.tier) is None
    assert escalate(a, top) == top


def test_escalate_moves_one_tier_up() -> None:
    a = claude_like()
    first = select_model(a, cls(category="debug"))  # default balanced
    second = escalate(a, first)
    assert second.tier == "deep"
    assert second.model == "claude-opus-5-5"
    assert second.model_rule == "escalation"
    assert second.effort == first.effort


def test_literal_id_never_escalates() -> None:
    a = agent(models={"balanced": "real"}, default={"model": "custom-literal"})
    decision = select_model(a, cls())
    assert decision.tier is None
    assert escalate(a, decision) == decision


def test_tier_omitted_from_explicit_escalation_list_never_escalates() -> None:
    a = agent(
        models={"fast": "f", "balanced": "b", "deep": "d"},
        default={"model": "balanced"},
        escalate_on_failure=["fast", "deep"],
    )
    # `balanced` is a valid tier but is absent from the explicit escalation list, so the
    # explicit list defines the escalation path and `balanced` never escalates.
    decision = select_model(a, cls())
    assert decision.tier == "balanced"
    assert next_escalation_tier(a, decision.tier) is None
    assert escalate(a, decision) == decision


# --- unavailable-model fallback ----------------------------------------------------------------


def test_available_model_does_not_fall_back() -> None:
    a = claude_like()
    decision = select_model(a, cls(category="debug"), available_models={"claude-sonnet-5-5"})
    # default balanced -> claude-sonnet-5-5 is available, so no fallback happens.
    assert decision.model == "claude-sonnet-5-5"
    assert decision.warning is None


def test_unavailable_matched_model_uses_default() -> None:
    a = claude_like()
    # claude-deep-review matches and resolves to claude-opus-5-5, which is unavailable.
    decision = select_model(
        a, cls(category="review", complexity="high"), available_models={"claude-sonnet-5-5"}
    )
    assert decision.model == "claude-sonnet-5-5"  # the default
    assert decision.tier == "balanced"
    assert decision.warning is not None
    assert "claude-opus-5-5" in decision.warning


def test_unavailable_model_and_unavailable_default() -> None:
    a = claude_like()
    # Both the matched model and the default are unavailable; the default is kept and the
    # warning names the final (still unavailable) model.
    decision = select_model(a, cls(category="review", complexity="high"), available_models=set())
    assert decision.model == "claude-sonnet-5-5"
    assert decision.tier == "balanced"
    assert decision.warning is not None
    assert "claude-opus-5-5" in decision.warning  # the matched model
    assert "claude-sonnet-5-5" in decision.warning  # the final, still-unavailable model
    assert "unavailable" in decision.warning


def test_literal_default_model_is_never_unavailable() -> None:
    codex = agent(models={"fast": "default", "deep": "default"}, default={"model": "deep"})
    decision = select_model(codex, cls(), available_models=set())
    assert decision.model == "default"
    assert decision.warning is None
