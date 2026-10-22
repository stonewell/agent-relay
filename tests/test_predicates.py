from __future__ import annotations

from typing import Any

import pytest

from orch.core.predicates import parse_when
from orch.core.specs import common


def matches(when: dict[str, Any], **facts: Any) -> bool:
    return parse_when(when).matches(facts)


def test_empty_when_matches_everything() -> None:
    assert matches({})


def test_attributes_are_a_conjunction() -> None:
    when = {"category": ["bugfix"], "language": ["python"]}
    assert matches(when, category="bugfix", language="python")
    assert not matches(when, category="bugfix", language="rust")
    assert not matches(when, category="test", language="python")


def test_list_values_are_a_disjunction() -> None:
    when = {"category": ["implement", "test", "migrate"]}
    assert matches(when, category="test")
    assert not matches(when, category="review")


def test_missing_fact_never_matches() -> None:
    assert not matches({"language": "python"}, category="bugfix")


def test_language_is_case_insensitive() -> None:
    assert matches({"language": "TypeScript"}, language="typescript")


@pytest.mark.parametrize(
    ("expr", "fact", "expected"),
    [
        (">=high", "high", True),
        (">=high", "medium", False),
        ("<=medium", "low", True),
        ("<=medium", "high", False),
        (">low", "medium", True),
        ("<medium", "medium", False),
        ("=medium", "medium", True),
        ("medium", "medium", True),
        ("medium", "high", False),
    ],
)
def test_complexity_is_ordered(expr: str, fact: str, expected: bool) -> None:
    assert matches({"complexity": expr}, complexity=fact) is expected


@pytest.mark.parametrize(
    ("expr", "fact", "expected"),
    [
        ("<5000", 4999, True),
        ("<5000", 5000, False),
        ("<=5000", 5000, True),
        (">100", 101, True),
        (">=100", 99, False),
        ("=3", 3, True),
        (3, 3, True),
        (3, 4, False),
        (" >= 2 ", 2, True),
    ],
)
def test_numeric_comparisons(expr: Any, fact: int, expected: bool) -> None:
    assert matches({"est_tokens": expr}, est_tokens=fact) is expected


def test_numeric_alternatives_are_a_disjunction() -> None:
    when = {"files": ["<2", ">=10"]}
    assert matches(when, files=1)
    assert matches(when, files=12)
    assert not matches(when, files=5)


def test_risk_values() -> None:
    assert matches({"risk": "elevated"}, risk="elevated")
    assert not matches({"risk": "elevated"}, risk="normal")


@pytest.mark.parametrize(
    "when",
    [
        {"colour": "red"},
        {"category": "poetry"},
        {"risk": "scary"},
        {"complexity": "huge"},
        {"complexity": ">=huge"},
        {"est_tokens": "many"},
        {"est_tokens": True},
        {"category": ">=implement"},
        {"language": "<python"},
        {"category": []},
        {"est_tokens": ""},
    ],
)
def test_invalid_when_is_rejected(when: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_when(when)


def test_when_must_be_a_mapping() -> None:
    with pytest.raises(ValueError, match="mapping"):
        parse_when(["category"])


def test_cost_ceiling_rejected_before_phase_4(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="available from phase 4"):
        parse_when({"cost_ceiling": "<5"})
    monkeypatch.setattr(common, "CURRENT_PHASE", 4)
    assert matches({"cost_ceiling": "<5"}, cost_ceiling=2.5)
