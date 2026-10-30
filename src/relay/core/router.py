"""Candidate scoring for automatic gateway model selection."""

from __future__ import annotations

from collections.abc import Mapping

from .classifier import Classification
from .registry import Agent

_COST_PENALTY = {"low": 0.0, "medium": 0.1, "high": 0.2}
_FAILURE_PENALTY = 0.5
_LOAD_PENALTY = 0.1


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


def score_candidate(
    name: str,
    agent: Agent,
    classification: Classification,
    load: Mapping[str, int],
    recent_failures: Mapping[str, int],
) -> float:
    """Rank a prequalified candidate without applying task permission filters."""
    return _score(name, agent, classification, load, recent_failures)
