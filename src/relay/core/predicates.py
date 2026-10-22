"""Rule predicate language for `when` blocks (design sections 3 and 4.3).

All specified attributes must match (conjunction). A list value matches if any element does
(disjunction). Numeric attributes and `complexity` accept comparison operators:
``<``, ``<=``, ``>``, ``>=`` and ``=``. A missing fact never matches.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast, get_args

from .specs.common import COST_FEATURE_PHASE, Category, require_phase

COMPLEXITY_ORDER = {"low": 0, "medium": 1, "high": 2}
CATEGORIES = frozenset(get_args(Category))
RISK_VALUES = frozenset({"normal", "elevated"})

NUMERIC_ATTRIBUTES = frozenset({"est_tokens", "files", "attempt", "cost_ceiling"})
ENUM_ATTRIBUTES = frozenset({"category", "risk"})
ATTRIBUTES = NUMERIC_ATTRIBUTES | ENUM_ATTRIBUTES | {"complexity", "language"}

_OPS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "=": operator.eq,
}
_EXPR = re.compile(r"^\s*(<=|>=|<|>|=)?\s*(\S.*?)\s*$")


@dataclass(frozen=True)
class _Compare:
    """One alternative of an attribute: compare the fact with a constant."""

    op: str
    value: float

    def matches(self, fact: float) -> bool:
        return _OPS[self.op](fact, self.value)


@dataclass(frozen=True)
class _Equals:
    value: str

    def matches(self, fact: str) -> bool:
        return fact == self.value


@dataclass(frozen=True)
class Predicate:
    """A compiled `when` block."""

    clauses: tuple[tuple[str, tuple[_Compare | _Equals, ...]], ...]

    def matches(self, facts: Mapping[str, Any]) -> bool:
        for attribute, alternatives in self.clauses:
            fact = facts.get(attribute)
            if fact is None or not _any_match(attribute, alternatives, fact):
                return False
        return True


def _any_match(attribute: str, alternatives: tuple[_Compare | _Equals, ...], fact: Any) -> bool:
    if attribute in NUMERIC_ATTRIBUTES:
        number = float(fact)
        return any(isinstance(a, _Compare) and a.matches(number) for a in alternatives)
    if attribute == "complexity":
        rank = COMPLEXITY_ORDER[str(fact)]
        return any(isinstance(a, _Compare) and a.matches(rank) for a in alternatives)
    text = str(fact).lower()
    return any(isinstance(a, _Equals) and a.matches(text) for a in alternatives)


def _split_op(raw: object, attribute: str) -> tuple[str, str]:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError(f"when.{attribute}: unsupported value {raw!r}")
    match = _EXPR.match(str(raw))
    if match is None:
        raise ValueError(f"when.{attribute}: empty value")
    return match.group(1) or "=", match.group(2)


def _parse_alternative(attribute: str, raw: object) -> _Compare | _Equals:
    op, text = _split_op(raw, attribute)
    if attribute in NUMERIC_ATTRIBUTES:
        try:
            return _Compare(op, float(text))
        except ValueError:
            raise ValueError(f"when.{attribute}: {text!r} is not a number") from None
    if attribute == "complexity":
        if text not in COMPLEXITY_ORDER:
            raise ValueError(f"when.complexity: {text!r} is not one of low, medium, high")
        return _Compare(op, COMPLEXITY_ORDER[text])
    if op != "=" or (isinstance(raw, str) and raw.lstrip()[:1] in "<>="):
        raise ValueError(f"when.{attribute}: comparison operators are not allowed here")
    if attribute == "category" and text not in CATEGORIES:
        raise ValueError(f"when.category: unknown category {text!r}")
    if attribute == "risk" and text not in RISK_VALUES:
        raise ValueError(f"when.risk: {text!r} is not normal or elevated")
    return _Equals(text.lower())


def parse_when(raw: object) -> Predicate:
    """Compile a `when` mapping; raises ValueError naming the offending attribute."""
    if not isinstance(raw, Mapping):
        raise ValueError("when must be a mapping")
    clauses: list[tuple[str, tuple[_Compare | _Equals, ...]]] = []
    for attribute, value in cast("Mapping[Any, Any]", raw).items():
        if attribute not in ATTRIBUTES:
            allowed = ", ".join(sorted(ATTRIBUTES))
            raise ValueError(f"when: unknown attribute {attribute!r} (allowed: {allowed})")
        if attribute == "cost_ceiling":
            require_phase("when.cost_ceiling", COST_FEATURE_PHASE)
        items: list[object] = (
            list(cast("list[object]", value)) if isinstance(value, list) else [value]
        )
        if not items:
            raise ValueError(f"when.{attribute}: an empty list can never match")
        clauses.append((attribute, tuple(_parse_alternative(attribute, i) for i in items)))
    return Predicate(tuple(clauses))
