"""Deterministic request category detection for automatic model selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Category = Literal[
    "implement",
    "bugfix",
    "refactor",
    "test",
    "review",
    "debug",
    "migrate",
    "docs-for-code",
    "codebase-research",
    "architecture",
]

CATEGORY_PRECEDENCE: tuple[Category, ...] = (
    "bugfix",
    "debug",
    "refactor",
    "test",
    "review",
    "migrate",
    "docs-for-code",
    "codebase-research",
    "architecture",
    "implement",
)

CATEGORY_SIGNALS: dict[Category, tuple[str, ...]] = {
    "implement": (
        "add",
        "build",
        "create",
        "implement",
        "support",
        "feature",
        "acceptance criteria",
    ),
    "bugfix": (
        "bug",
        "crash",
        "regression",
        "stack trace",
        "traceback",
        "failing test",
        "fix",
        "broken",
    ),
    "refactor": (
        "refactor",
        "rename",
        "extract",
        "restructure",
        "no behaviour change",
        "no behavior change",
        "simplify",
        "move",
    ),
    "test": (
        "write tests",
        "add tests",
        "unit tests",
        "integration tests",
        "coverage",
        "flaky test",
        "test suite",
    ),
    "review": (
        "review",
        "pull request",
        "merge request",
        "pr",
        "diff",
        "branch",
    ),
    "debug": (
        "why does",
        "why is",
        "debug",
        "logs",
        "log output",
        "repro steps",
        "reproduce",
        "investigate",
    ),
    "migrate": (
        "migrate",
        "upgrade",
        "bump",
        "dependency upgrade",
        "framework upgrade",
        "port to",
    ),
    "docs-for-code": (
        "docstring",
        "docstrings",
        "readme",
        "api docs",
        "documentation",
        "document",
        "comments",
    ),
    "codebase-research": (
        "where is",
        "where are",
        "how does",
        "how do",
        "find",
        "locate",
        "explain how",
        "research",
    ),
    "architecture": (
        "design",
        "architecture",
        "plan",
        "spec",
        "rfc",
        "proposal",
        "system design",
    ),
}

_WORD_CHAR = "0-9a-z_"


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![{_WORD_CHAR}]){re.escape(phrase)}(?![{_WORD_CHAR}])",
        re.IGNORECASE,
    )


_CATEGORY_PATTERNS: dict[Category, tuple[re.Pattern[str], ...]] = {
    category: tuple(_phrase_pattern(phrase) for phrase in phrases)
    for category, phrases in CATEGORY_SIGNALS.items()
}


def _matches_phrase(text: str, pattern: re.Pattern[str]) -> bool:
    return pattern.search(text) is not None


def detect_category(request: str) -> Category:
    """Return the best-scoring category; the precedence table breaks ties."""
    scores: dict[Category, int] = {
        category: sum(_matches_phrase(request, pattern) for pattern in patterns)
        for category, patterns in _CATEGORY_PATTERNS.items()
    }
    best = max(scores.values())
    if best == 0:
        return "implement"
    for category in CATEGORY_PRECEDENCE:
        if scores[category] == best:
            return category
    raise AssertionError("unreachable: every category is in CATEGORY_PRECEDENCE")


@dataclass(frozen=True)
class Classification:
    category: Category


def classify(request: str) -> Classification:
    return Classification(category=detect_category(request))
