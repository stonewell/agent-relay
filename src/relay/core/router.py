"""Agent router (design section 4.2).

Picks the agent for a task: a pinned agent, the first matching ``routes.rules`` rule in
effective priority order, or a category/strength score fallback. Candidates are filtered
for availability, permissions and model capability; eligible candidates without capacity
queue rather than being declared unavailable, and an exhausted preference list falls back
to scoring with the reason recorded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .classifier import Classification
from .registry import Agent, Config, effective_rules
from .specs.common import PermissionLevel
from .specs.route import Alternative

IMPLICIT_IDS = frozenset(
    {"pinned", "category-match", "score", "llm-classifier", "fallback", "escalation"}
)
"""Fixed implicit rule ids (design section 4.2)."""

_COST_PENALTY = {"low": 0.0, "medium": 0.1, "high": 0.2}
_FAILURE_PENALTY = 0.5
_LOAD_PENALTY = 0.1


@dataclass(frozen=True)
class Excluded:
    """A preferred candidate that was filtered out."""

    agent: str
    reason: str


@dataclass(frozen=True)
class AgentDecision:
    """The agent chosen for one attempt, plus the evidence for that choice."""

    agent: str | None
    agent_rule: str
    reason: str
    alternatives: tuple[Alternative, ...] = ()
    busy: tuple[str, ...] = ()
    exhausted: tuple[Excluded, ...] = ()
    queued: bool = False
    rejected: bool = False
    permissions: PermissionLevel = "full"


def _facts(
    classification: Classification, est_tokens: int, files: int, attempt: int
) -> dict[str, object]:
    return {
        "category": classification.category,
        "complexity": classification.complexity,
        "risk": classification.risk,
        "language": classification.language,
        "est_tokens": est_tokens,
        "files": files,
        "attempt": attempt,
    }


def _capacity(agent: Agent, config: Config) -> int:
    return agent.concurrency if agent.concurrency is not None else config.defaults.concurrency


def _eligible(
    name: str,
    agent: Agent,
    available: Mapping[str, bool] | None,
    levels: Mapping[str, Sequence[PermissionLevel]] | None,
    permissions: PermissionLevel,
) -> tuple[bool, str]:
    if not agent.enabled:
        return False, "disabled"
    if available is not None and not available.get(name, True):
        return False, "unavailable"
    allowed = levels.get(name, ("full",)) if levels is not None else ("full",)
    if permissions not in allowed:
        return False, f"permission {permissions} unsupported"
    if not agent.models:
        return False, "no models"
    return True, ""


def _score(
    name: str,
    agent: Agent,
    classification: Classification,
    load: Mapping[str, int],
    recent_failures: Mapping[str, int],
) -> float:
    category = 1.0 if classification.category in agent.categories else 0.0
    # `strengths` has no deterministic signal mapping yet (section 4.1 does not derive one);
    # it contributes 0 so scoring stays fully deterministic.
    strengths = 0.0
    cost = _COST_PENALTY[agent.cost] if agent.cost is not None else 0.0
    failures = recent_failures.get(name, 0)
    running = load.get(name, 0)
    return category + strengths - cost - _FAILURE_PENALTY * failures - _LOAD_PENALTY * running


def _fallback_order(config: Config, name: str) -> tuple[int, int]:
    """Tie-break: `defaults.fallback` order first, then registry declaration order."""
    fallback = config.defaults.fallback
    if name in fallback:
        return (0, fallback.index(name))
    return (1, tuple(config.agents).index(name))


def _pick_reason(
    name: str,
    agent: Agent,
    config: Config,
    load: Mapping[str, int],
    rule: str,
    classification: Classification,
) -> str:
    capacity = _capacity(agent, config)
    return (
        f"category={classification.category} matched {rule}; {name} preferred, "
        f"load {load.get(name, 0)}/{capacity}"
    )


def _evaluate(
    names: Sequence[str],
    rule: str,
    config: Config,
    classification: Classification,
    available: Mapping[str, bool] | None,
    levels: Mapping[str, Sequence[PermissionLevel]] | None,
    load: Mapping[str, int],
    permissions: PermissionLevel,
) -> AgentDecision:
    exhausted: list[Excluded] = []
    busy: list[str] = []
    for name in names:
        agent = config.agents[name]
        ok, reason = _eligible(name, agent, available, levels, permissions)
        if not ok:
            exhausted.append(Excluded(name, reason))
            continue
        if load.get(name, 0) < _capacity(agent, config):
            return AgentDecision(
                agent=name,
                agent_rule=rule,
                reason=_pick_reason(name, agent, config, load, rule, classification),
                busy=tuple(busy),
                exhausted=tuple(exhausted),
                permissions=permissions,
            )
        busy.append(name)
    if busy:
        name = busy[0]
        agent = config.agents[name]
        return AgentDecision(
            agent=name,
            agent_rule=rule,
            reason=_pick_reason(name, agent, config, load, rule, classification),
            busy=tuple(busy),
            exhausted=tuple(exhausted),
            queued=True,
            permissions=permissions,
        )
    return AgentDecision(
        agent=None,
        agent_rule=rule,
        reason=f"no eligible agent from {list(names)}",
        exhausted=tuple(exhausted),
        rejected=True,
        permissions=permissions,
    )


def _score_decision(
    config: Config,
    classification: Classification,
    available: Mapping[str, bool] | None,
    levels: Mapping[str, Sequence[PermissionLevel]] | None,
    load: Mapping[str, int],
    permissions: PermissionLevel,
    recent_failures: Mapping[str, int],
    *,
    exhausted_prefix: tuple[Excluded, ...] = (),
    note: str = "",
) -> AgentDecision:
    ranked: list[tuple[str, float]] = []
    for name, agent in config.agents.items():
        ok, _ = _eligible(name, agent, available, levels, permissions)
        if not ok:
            continue
        ranked.append((name, _score(name, agent, classification, load, recent_failures)))
    if not ranked:
        return AgentDecision(
            agent=None,
            agent_rule="score",
            reason="no eligible agent" if not note else f"{note}; no eligible agent",
            exhausted=exhausted_prefix,
            rejected=True,
            permissions=permissions,
        )
    ranked.sort(key=lambda item: (-item[1], _fallback_order(config, item[0])))
    winner = ranked[0][0]
    category_winners = [
        n for n, _ in ranked if classification.category in config.agents[n].categories
    ]
    rule = (
        "category-match"
        if (len(category_winners) == 1 and winner == category_winners[0])
        else "score"
    )

    def reason_for(name: str, score: float) -> str:
        base = f"{name} preferred by {rule} (score {score:.2f})"
        return f"{note}; {base}" if note else base

    busy: list[str] = []
    for name, score in ranked:
        agent = config.agents[name]
        if load.get(name, 0) < _capacity(agent, config):
            alternatives = tuple(
                Alternative(agent=n, score=round(s, 2)) for n, s in ranked if n != name
            )
            return AgentDecision(
                agent=name,
                agent_rule=rule,
                reason=reason_for(name, score),
                alternatives=alternatives,
                busy=tuple(busy),
                exhausted=exhausted_prefix,
                permissions=permissions,
            )
        busy.append(name)
    name = busy[0]
    score = dict(ranked)[name]
    alternatives = tuple(Alternative(agent=n, score=round(s, 2)) for n, s in ranked if n != name)
    return AgentDecision(
        agent=name,
        agent_rule=rule,
        reason=reason_for(name, score) + " but busy",
        alternatives=alternatives,
        busy=tuple(busy),
        exhausted=exhausted_prefix,
        queued=True,
        permissions=permissions,
    )


def route_agent(
    config: Config,
    classification: Classification,
    *,
    pinned: str | None = None,
    available: Mapping[str, bool] | None = None,
    levels: Mapping[str, Sequence[PermissionLevel]] | None = None,
    load: Mapping[str, int] | None = None,
    permissions: PermissionLevel = "full",
    recent_failures: Mapping[str, int] | None = None,
    est_tokens: int = 0,
    files: int = 0,
    attempt: int = 1,
) -> AgentDecision:
    """Route one attempt.

    ``available`` is the probe result per agent (missing keys mean available); ``levels`` is
    each agent's enforceable permission levels from ``probe().levels`` (missing keys mean
    ``full`` only); ``load`` is the current running-attempt count per agent.
    """
    load_map = load or {}
    recent = recent_failures or {}
    if pinned is not None:
        if pinned not in config.agents:
            return AgentDecision(
                agent=None,
                agent_rule="pinned",
                reason=f"unknown agent {pinned!r}",
                rejected=True,
                permissions=permissions,
            )
        return _evaluate(
            (pinned,), "pinned", config, classification, available, levels, load_map, permissions
        )
    facts = _facts(classification, est_tokens, files, attempt)
    for rule in effective_rules(config.routes.rules):
        if rule.predicate().matches(facts):
            decision = _evaluate(
                tuple(rule.prefer),
                rule.id,
                config,
                classification,
                available,
                levels,
                load_map,
                permissions,
            )
            if not decision.rejected:
                return decision
            return _score_decision(
                config,
                classification,
                available,
                levels,
                load_map,
                permissions,
                recent,
                exhausted_prefix=decision.exhausted,
                note=f"rule {rule.id} preferred list exhausted",
            )
    return _score_decision(config, classification, available, levels, load_map, permissions, recent)
