"""Model selection inside an agent (design section 4.3).

Every agent owns an independent ``model_routing`` block. :func:`select_model` evaluates
that agent's enabled rules in effective priority order and returns the resolved model, its
logical tier (for escalation), the reasoning effort and the matching rule id. :func:`escalate`
moves a failed attempt one tier up the agent's escalation list; a literal model id or the
top tier never escalates.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from .classifier import Classification
from .registry import Agent, effective_rules

NO_MODEL = "default"
"""Literal model id meaning "pass no model flag" (design section 3)."""


@dataclass(frozen=True)
class ModelDecision:
    """One resolved model choice for an agent."""

    model: str
    """Resolved model id for the adapter and route log: a tier name resolved through
    ``models``, or a literal id."""

    tier: str | None
    """Logical tier the model came from; ``None`` when a literal id was selected."""

    effort: str | None
    model_rule: str
    """Matching ``model_routing.rules`` id, ``"default"``, or ``"escalation"``."""

    warning: str | None = None
    """Set when the resolved model was unavailable and the default was used instead."""


def escalation_tiers(agent: Agent) -> tuple[str, ...]:
    """The escalation order, lowest to highest (design section 4.3)."""
    return tuple(agent.tiers())


def _resolve(agent: Agent, selection: str) -> tuple[str, str | None]:
    """Resolve a tier name through ``models``, or keep a literal id; return ``(model, tier)``."""
    if selection in agent.models:
        return agent.models[selection], selection
    return selection, None


def _default_selection(agent: Agent) -> tuple[str, str | None]:
    """The default ``model`` and ``effort``; the first tier when no default is configured."""
    default = agent.model_routing.default
    if default is not None:
        return default.model, default.effort
    tiers = list(agent.models)
    if tiers:
        return tiers[0], None
    return NO_MODEL, None


def _is_unavailable(model: str, available: Collection[str] | None) -> bool:
    if available is None or model == NO_MODEL:
        return False
    return model not in available


def _finalize(
    agent: Agent,
    selection: str,
    effort: str | None,
    model_rule: str,
    available_models: Collection[str] | None,
) -> ModelDecision:
    model, tier = _resolve(agent, selection)
    if not _is_unavailable(model, available_models):
        return ModelDecision(model, tier, effort, model_rule)
    fallback_selection, fallback_effort = _default_selection(agent)
    fallback_model, fallback_tier = _resolve(agent, fallback_selection)
    if _is_unavailable(fallback_model, available_models):
        warning = f"model {model!r} unavailable; default {fallback_model!r} is also unavailable"
    else:
        warning = f"model {model!r} unavailable; using default {fallback_model!r}"
    return ModelDecision(fallback_model, fallback_tier, fallback_effort, model_rule, warning)


def select_model(
    agent: Agent,
    classification: Classification,
    *,
    est_tokens: int = 0,
    files: int = 0,
    attempt: int = 1,
    available_models: Collection[str] | None = None,
) -> ModelDecision:
    """Pick the model, tier, effort and rule for one attempt (first match wins, else default)."""
    facts = {
        "category": classification.category,
        "complexity": classification.complexity,
        "risk": classification.risk,
        "language": classification.language,
        "est_tokens": est_tokens,
        "files": files,
        "attempt": attempt,
    }
    for rule in effective_rules(agent.model_routing.rules):
        if rule.predicate().matches(facts):
            return _finalize(agent, rule.use.model, rule.use.effort, rule.id, available_models)
    selection, effort = _default_selection(agent)
    return _finalize(agent, selection, effort, "default", available_models)


def next_escalation_tier(agent: Agent, current_tier: str | None) -> str | None:
    """The tier one step up from ``current_tier``, or ``None`` at the ceiling.

    A literal id (``current_tier`` is ``None``) never escalates, and neither does a tier
    that is not in the escalation list (design section 4.3).
    """
    tiers = escalation_tiers(agent)
    if current_tier is None or current_tier not in tiers:
        return None
    index = tiers.index(current_tier)
    if index == len(tiers) - 1:
        return None
    return tiers[index + 1]


def escalate(agent: Agent, current: ModelDecision) -> ModelDecision:
    """The model for a retry after a failure: one tier up, or unchanged at the ceiling."""
    nxt = next_escalation_tier(agent, current.tier)
    if nxt is None:
        return current
    model, _ = _resolve(agent, nxt)
    return ModelDecision(model, nxt, current.effort, "escalation")
