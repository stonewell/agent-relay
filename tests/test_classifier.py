"""Category fixtures for automatic gateway routing."""

from __future__ import annotations

import pytest

from relay.core.classifier import Category, classify


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "implement"),
        ("Add a CSV export feature with acceptance criteria", "implement"),
        ("Fix crash in the parser", "bugfix"),
        ("Rename the config loader without behavior change", "refactor"),
        ("Write tests for the tokenizer", "test"),
        ("Review the diff on branch feature/x", "review"),
        ("Why does the scheduler deadlock? Logs and repro steps attached", "debug"),
        ("Upgrade the project to Python 3.12", "migrate"),
        ("Update the README and docstrings for the API", "docs-for-code"),
        ("Where is the retry policy implemented?", "codebase-research"),
        ("Design the architecture for a plugin system", "architecture"),
        ("Rotate the AUTH credentials for the service", "implement"),
        ("Update author metadata in the package", "implement"),
    ],
)
def test_category_fixtures(text: str, expected: Category) -> None:
    assert classify(text).category == expected


def test_classification_is_deterministic() -> None:
    assert classify("Fix crash in src/auth/login.py") == classify("Fix crash in src/auth/login.py")
