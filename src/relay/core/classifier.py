"""Deterministic task classifier (design section 4.1).

The classifier derives a category, complexity, risk level and resolved language from the
request text and the signals named by design section 4.1. It is pure: no randomness, no
clock, no environment reads and no I/O.

Category detection scores the request against the ``CATEGORY_SIGNALS`` table. Complexity is
the maximum of the estimates for touched units, cross-module reach, estimated context
tokens, diff size and prior failures; an ``elevated`` risk raises it by one level, capped at
``high``. Risk is ``elevated`` when a path in ``files``, ``owns`` or ``diff_scope`` matches a
configured path glob, or when the request names a configured keyword. Language is resolved
hierarchically: explicit language, then the dominant known extension in ``files``/``owns``,
then the repository language.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal

from .specs.common import Category, Complexity

__all__ = [
    "CATEGORY_PRECEDENCE",
    "CATEGORY_SIGNALS",
    "COMPLEXITY_LEVELS",
    "DEFAULT_RISK_KEYWORDS",
    "DEFAULT_RISK_PATHS",
    "EXTENSION_LANGUAGES",
    "Classification",
    "RiskLevel",
    "classify",
    "detect_category",
    "detect_risk",
    "estimate_complexity",
    "match_path_glob",
]

RiskLevel = Literal["normal", "elevated"]

COMPLEXITY_LEVELS: tuple[Complexity, ...] = ("low", "medium", "high")

# Tie-break order for equally scoring categories: narrower tasks before the general
# ``implement`` fallback. Design section 4.1 does not state a precedence.
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

# Design section 4.1 defaults (also present in built-in agents.yaml). A caller may pass a
# different sequence, including an empty one to disable path-based risk.
DEFAULT_RISK_PATHS: tuple[str, ...] = (
    "**/auth/**",
    "**/security/**",
    "**/migrations/**",
    ".github/workflows/**",
    "**/Dockerfile*",
    "**/infra/**",
    "**/*.tf",
)

# Design section 4.1 names "configured risk keywords" without defining their source (the
# configuration has no keyword key). These defaults are the sensitive-domain terms the
# design's default globs describe; callers can supply their own sequence.
DEFAULT_RISK_KEYWORDS: tuple[str, ...] = (
    "auth",
    "authentication",
    "authorization",
    "credentials",
    "password",
    "secret",
    "secret rotation",
    "security",
    "migration",
    "migrations",
    "infrastructure",
    "terraform",
    "deployment",
)

# Extension -> language. The first matching extension in the path wins (``.d.ts`` uses
# ``.ts``); unknown extensions are ignored during inference.
EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
    ".tf": "terraform",
    ".hcl": "terraform",
    ".md": "markdown",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".vue": "vue",
    ".svelte": "svelte",
}

# Unit boundaries: at most the first value is low, at most the second is medium.
_UNIT_LOW = 2
_UNIT_MEDIUM = 8
_MODULE_LOW = 1
_MODULE_MEDIUM = 3
_TOKEN_LOW = 8_000
_TOKEN_MEDIUM = 32_000
_DIFF_LOW = 100
_DIFF_MEDIUM = 1_000
_FAILURE_MEDIUM = 1

_DRIVE = re.compile(r"^[A-Za-z]:")

# A signal must be a whole word/phrase: "add" does not match "address".
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


def _split_path(value: str) -> tuple[str, ...]:
    """Normalise a repository-relative path or glob into segments."""
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or _DRIVE.match(text):
        raise ValueError(f"{value!r} must be relative to the repository root")
    parts = tuple(part for part in text.split("/") if part not in ("", "."))
    if ".." in parts:
        raise ValueError(f"{value!r} must not contain '..'")
    return parts


def match_path_glob(path: str, pattern: str) -> bool:
    """True if repository-relative ``path`` matches ``pattern``.

    ``**`` matches zero or more path segments, ``*``, ``?`` and ``[...]`` match within one
    segment. Matching is case-sensitive so the result is identical on every host.
    """
    return _match_segments(_split_path(path), _split_path(pattern))


def _match_segments(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        if len(pattern) == 1:
            return True
        return _match_segments(path, pattern[1:]) or (
            bool(path) and _match_segments(path[1:], pattern)
        )
    if not path:
        return False
    if not fnmatchcase(path[0], head):
        return False
    return _match_segments(path[1:], pattern[1:])


def _positive(name: str, value: int) -> int:
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


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


def _risk_rank(level: RiskLevel) -> int:
    return 1 if level == "elevated" else 0


def _level(rank: int) -> Complexity:
    return COMPLEXITY_LEVELS[min(rank, 2)]


def _bounded_rank(value: int, low: int, medium: int) -> int:
    if value <= low:
        return 0
    if value <= medium:
        return 1
    return 2


def _module_count(paths: Iterable[str]) -> int:
    modules: set[str] = set()
    for path in paths:
        segments = _split_path(path)
        if len(segments) > 1:
            modules.add(segments[0])
    return len(modules)


def estimate_complexity(
    *,
    files: Iterable[str] = (),
    owns: Iterable[str] = (),
    symbols: Iterable[str] = (),
    est_tokens: int = 0,
    diff_lines: int = 0,
    prior_failures: int = 0,
    risk: RiskLevel = "normal",
) -> Complexity:
    """Estimate complexity from the signals named by design section 4.1."""
    file_list = tuple(files)
    own_list = tuple(owns)
    symbol_list = tuple(symbols)
    _positive("est_tokens", est_tokens)
    _positive("diff_lines", diff_lines)
    _positive("prior_failures", prior_failures)

    units = len(set(file_list) | set(own_list) | set(symbol_list))
    modules = _module_count((*file_list, *own_list))
    rank = max(
        _bounded_rank(units, _UNIT_LOW, _UNIT_MEDIUM),
        _bounded_rank(modules, _MODULE_LOW, _MODULE_MEDIUM),
        _bounded_rank(est_tokens, _TOKEN_LOW, _TOKEN_MEDIUM),
        _bounded_rank(diff_lines, _DIFF_LOW, _DIFF_MEDIUM),
        _bounded_rank(prior_failures, 0, _FAILURE_MEDIUM),
    )
    return _level(rank + _risk_rank(risk))


def _normalise_language(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    return text or None


def _path_language(path: str) -> str | None:
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    return EXTENSION_LANGUAGES.get(suffix)


def _detected_languages(paths: Iterable[str]) -> tuple[str, ...]:
    """Known languages in dominance order (count desc, first appearance)."""
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for index, path in enumerate(paths):
        language = _path_language(path)
        if language is None:
            continue
        counts[language] = counts.get(language, 0) + 1
        first_seen.setdefault(language, index)
    ordered = sorted(counts, key=lambda language: (-counts[language], first_seen[language]))
    return tuple(ordered)


def detect_risk(
    request: str,
    *,
    files: Iterable[str] = (),
    owns: Iterable[str] = (),
    diff_scope: Iterable[str] = (),
    risk_paths: Sequence[str] = DEFAULT_RISK_PATHS,
    risk_keywords: Sequence[str] = DEFAULT_RISK_KEYWORDS,
) -> tuple[RiskLevel, tuple[str, ...]]:
    """Return the risk level and the deterministic evidence for it."""
    evidence: list[str] = []
    for path in (*tuple(files), *tuple(owns), *tuple(diff_scope)):
        for pattern in risk_paths:
            if match_path_glob(path, pattern):
                evidence.append(f"{path} matches {pattern}")
                break
    for keyword in risk_keywords:
        if _matches_phrase(request, _phrase_pattern(keyword)):
            evidence.append(f"keyword {keyword!r}")
    return ("elevated" if evidence else "normal"), tuple(evidence)


def classify(
    request: str,
    *,
    files: Iterable[str] = (),
    owns: Iterable[str] = (),
    diff_scope: Iterable[str] = (),
    symbols: Iterable[str] = (),
    language: str | None = None,
    repo_language: str | None = None,
    est_tokens: int = 0,
    diff_lines: int = 0,
    prior_failures: int = 0,
    risk_paths: Sequence[str] = DEFAULT_RISK_PATHS,
    risk_keywords: Sequence[str] = DEFAULT_RISK_KEYWORDS,
) -> Classification:
    """Classify a task request and the signals named by design section 4.1."""
    file_list = tuple(files)
    own_list = tuple(owns)
    diff_scope_list = tuple(diff_scope)
    symbol_list = tuple(symbols)

    category = detect_category(request)
    risk, risk_evidence = detect_risk(
        request,
        files=file_list,
        owns=own_list,
        diff_scope=diff_scope_list,
        risk_paths=risk_paths,
        risk_keywords=risk_keywords,
    )
    complexity = estimate_complexity(
        files=file_list,
        owns=own_list,
        symbols=symbol_list,
        est_tokens=est_tokens,
        diff_lines=diff_lines,
        prior_failures=prior_failures,
        risk=risk,
    )
    languages = _detected_languages((*file_list, *own_list))
    resolved = _normalise_language(language) or (
        languages[0] if languages else _normalise_language(repo_language)
    )
    return Classification(
        category=category,
        complexity=complexity,
        risk=risk,
        language=resolved,
        languages=languages,
        risk_evidence=risk_evidence,
    )


@dataclass(frozen=True)
class Classification:
    """Result of :func:`classify` (design section 4.1)."""

    category: Category
    complexity: Complexity
    risk: RiskLevel
    language: str | None
    languages: tuple[str, ...] = ()
    risk_evidence: tuple[str, ...] = ()
