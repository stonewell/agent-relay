"""Fixture table for the deterministic classifier (design section 4.1)."""

from __future__ import annotations

from typing import Any

import pytest

from relay.core.classifier import (
    Classification,
    RiskLevel,
    classify,
    estimate_complexity,
    match_path_glob,
)
from relay.core.specs.common import Category, Complexity


def classification(
    category: Category,
    complexity: Complexity,
    risk: RiskLevel = "normal",
    language: str | None = None,
    languages: tuple[str, ...] = (),
    risk_evidence: tuple[str, ...] = (),
) -> Classification:
    return Classification(
        category=category,
        complexity=complexity,
        risk=risk,
        language=language,
        languages=languages,
        risk_evidence=risk_evidence,
    )


CASES: list[Any] = [
    pytest.param(
        "",
        {},
        classification("implement", "low"),
        id="empty-request",
    ),
    pytest.param(
        "Add a CSV export feature with acceptance criteria",
        {},
        classification("implement", "low"),
        id="category-implement",
    ),
    pytest.param(
        "Fix crash in the parser",
        {},
        classification("bugfix", "low"),
        id="category-bugfix",
    ),
    pytest.param(
        "Rename the config loader without behavior change",
        {},
        classification("refactor", "low"),
        id="category-refactor",
    ),
    pytest.param(
        "Write tests for the tokenizer",
        {},
        classification("test", "low"),
        id="category-test",
    ),
    pytest.param(
        "Review the diff on branch feature/x",
        {},
        classification("review", "low"),
        id="category-review",
    ),
    pytest.param(
        "Why does the scheduler deadlock? Logs and repro steps attached",
        {},
        classification("debug", "low"),
        id="category-debug",
    ),
    pytest.param(
        "Upgrade the project to Python 3.12",
        {},
        classification("migrate", "low"),
        id="category-migrate",
    ),
    pytest.param(
        "Update the README and docstrings for the API",
        {},
        classification("docs-for-code", "low"),
        id="category-docs-for-code",
    ),
    pytest.param(
        "Where is the retry policy implemented?",
        {},
        classification("codebase-research", "low"),
        id="category-codebase-research",
    ),
    pytest.param(
        "Design the architecture for a plugin system",
        {},
        classification("architecture", "low"),
        id="category-architecture",
    ),
    # Complexity boundaries.
    pytest.param(
        "Add a parser",
        {"files": ("src/a.py",), "est_tokens": 8_000, "diff_lines": 100},
        classification("implement", "low", language="python", languages=("python",)),
        id="complexity-low-boundaries",
    ),
    pytest.param(
        "Add a parser",
        {"est_tokens": 8_001},
        classification("implement", "medium"),
        id="complexity-medium-token-boundary",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("src/a.py", "src/b.py", "src/c.py")},
        classification("implement", "medium", language="python", languages=("python",)),
        id="complexity-medium-file-boundary",
    ),
    pytest.param(
        "Add a parser",
        {
            "files": (
                "src/a.py",
                "src/b.py",
                "src/c.py",
                "lib/d.py",
                "lib/e.py",
                "lib/f.py",
                "app/g.py",
                "app/h.py",
                "docs/i.py",
            )
        },
        classification("implement", "high", language="python", languages=("python",)),
        id="complexity-high-unit-boundary",
    ),
    pytest.param(
        "Add a parser",
        {"diff_lines": 1_001},
        classification("implement", "high"),
        id="complexity-high-diff-boundary",
    ),
    pytest.param(
        "Add a parser",
        {"prior_failures": 2},
        classification("implement", "high"),
        id="complexity-high-prior-failures",
    ),
    # Risk: glob paths, keywords, custom lists and the complexity bump.
    pytest.param(
        "Add a parser",
        {"files": ("src/auth/login.py",), "est_tokens": 8_001},
        classification(
            "implement",
            "high",
            "elevated",
            "python",
            ("python",),
            ("src/auth/login.py matches **/auth/**",),
        ),
        id="risk-bumps-complexity",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("src/auth/login.py",)},
        classification(
            "implement",
            "medium",
            "elevated",
            "python",
            ("python",),
            ("src/auth/login.py matches **/auth/**",),
        ),
        id="risk-glob-files",
    ),
    pytest.param(
        "Add a parser",
        {"owns": ("src/security/policy.ts",)},
        classification(
            "implement",
            "medium",
            "elevated",
            "typescript",
            ("typescript",),
            ("src/security/policy.ts matches **/security/**",),
        ),
        id="risk-glob-owns",
    ),
    pytest.param(
        "Add a parser",
        {"diff_scope": (".github/workflows/ci.yml",)},
        classification(
            "implement",
            "medium",
            "elevated",
            risk_evidence=(".github/workflows/ci.yml matches .github/workflows/**",),
        ),
        id="risk-glob-diff-scope",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("Dockerfile",)},
        classification(
            "implement",
            "medium",
            "elevated",
            risk_evidence=("Dockerfile matches **/Dockerfile*",),
        ),
        id="risk-glob-dockerfile",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("infra/main.tf",)},
        classification(
            "implement",
            "medium",
            "elevated",
            "terraform",
            ("terraform",),
            ("infra/main.tf matches **/infra/**",),
        ),
        id="risk-glob-infra",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("src/payments/charge.py",)},
        classification("implement", "low", language="python", languages=("python",)),
        id="risk-unknown-path-is-normal",
    ),
    pytest.param(
        "Rotate the AUTH credentials for the service",
        {},
        classification(
            "implement",
            "medium",
            "elevated",
            risk_evidence=("keyword 'auth'", "keyword 'credentials'"),
        ),
        id="risk-keyword-case-insensitive",
    ),
    pytest.param(
        "Update author metadata in the package",
        {},
        classification("implement", "low"),
        id="risk-keyword-is-whole-word",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("src/payments/charge.py",), "risk_paths": ("**/payments/**",)},
        classification(
            "implement",
            "medium",
            "elevated",
            "python",
            ("python",),
            ("src/payments/charge.py matches **/payments/**",),
        ),
        id="risk-custom-glob-replaces-defaults",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("src/auth/login.py",), "risk_paths": ()},
        classification("implement", "low", language="python", languages=("python",)),
        id="risk-empty-glob-list-disables-path-risk",
    ),
    # Language resolution.
    pytest.param(
        "Add a parser",
        {"files": ("src/a.py",), "language": "TypeScript"},
        classification("implement", "low", language="typescript", languages=("python",)),
        id="language-explicit-wins",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("src/a.ts", "src/b.ts", "src/c.py")},
        classification(
            "implement",
            "medium",
            language="typescript",
            languages=("typescript", "python"),
        ),
        id="language-dominant-extension",
    ),
    pytest.param(
        "Add a parser",
        {"owns": ("lib/mod.rs",)},
        classification("implement", "low", language="rust", languages=("rust",)),
        id="language-owns-fallback",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("Makefile",), "repo_language": "Go"},
        classification("implement", "low", language="go"),
        id="language-repo-fallback",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("b.py", "a.ts")},
        classification(
            "implement",
            "low",
            language="python",
            languages=("python", "typescript"),
        ),
        id="language-tie-first-appearance",
    ),
    pytest.param(
        "Add a parser",
        {"files": ("assets/logo.xyz",)},
        classification("implement", "low"),
        id="language-unknown-extension",
    ),
]


@pytest.mark.parametrize(("text", "kwargs", "expected"), CASES)
def test_classification_fixtures(
    text: str, kwargs: dict[str, Any], expected: Classification
) -> None:
    assert classify(text, **kwargs) == expected


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("auth/login.py", "**/auth/**", True),
        ("src/auth/login.py", "**/auth/**", True),
        ("./src//auth/login.py", "**/auth/**", True),
        ("src\\auth\\login.py", "**/auth/**", True),
        ("src/authors.py", "**/auth/**", False),
        ("Dockerfile", "**/Dockerfile*", True),
        ("src/Dockerfile.dev", "**/Dockerfile*", True),
        ("main.tf", "**/*.tf", True),
        ("infra/main.tf", "**/infra/**", True),
        (".github/workflows/ci.yml", ".github/workflows/**", True),
        ("src/payments/charge.py", "**/auth/**", False),
    ],
)
def test_match_path_glob(path: str, pattern: str, expected: bool) -> None:
    assert match_path_glob(path, pattern) is expected


@pytest.mark.parametrize("path", ["/etc/passwd", "C:/repo/auth/x.py", "../auth/x.py"])
def test_match_path_glob_rejects_absolute_and_parent_paths(path: str) -> None:
    with pytest.raises(ValueError):
        match_path_glob(path, "**/auth/**")


def test_complexity_risk_bump_is_capped() -> None:
    assert (
        estimate_complexity(
            files=("src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/e.py"),
            risk="elevated",
        )
        == "high"
    )


def test_classification_is_deterministic() -> None:
    first = classify(
        "Fix crash in src/auth/login.py",
        files=("src/auth/login.py",),
        est_tokens=9_000,
        prior_failures=1,
    )
    second = classify(
        "Fix crash in src/auth/login.py",
        files=("src/auth/login.py",),
        est_tokens=9_000,
        prior_failures=1,
    )
    assert first == second
