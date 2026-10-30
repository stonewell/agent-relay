"""Coordinator response contracts (task 2.20; design 5.6, 7.4)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from relay.core.coordinator import CoordinationLimits
from relay.core.specs import SCHEMAS, CoordinatorResponse, parse_spec, schema_for

GRAPH: dict[str, Any] = {
    "nodes": [
        {
            "id": "n1",
            "category": "implement",
            "goal": "do it",
            "acceptance": [{"run": "true", "expect_exit": 0}],
        }
    ]
}


def parse(response: dict[str, Any]) -> CoordinatorResponse:
    return CoordinatorResponse.model_validate({"spec_version": 1, "response": response})


# --- response contract ---------------------------------------------------------------------


def test_each_action_validates() -> None:
    parse({"action": "ask_user", "questions": [{"id": "q1", "text": "Which?"}]})
    parse(
        {
            "action": "request_context",
            "reason": "need the handler",
            "selections": [
                {"kind": "repo", "path": "src/a.py", "lines": {"start": 1, "end": 20}},
                {
                    "kind": "artifact",
                    "run_id": "2026-10-22-a1b2",
                    "node_id": "n1",
                    "attempt": 1,
                    "artifact": "artifacts/diff.patch",
                    "sha256": "a" * 64,
                },
            ],
        }
    )
    parse({"action": "propose_plan", "explanation": "two steps", "graph": GRAPH})
    parse(
        {
            "action": "review",
            "summary": "ok",
            "findings": [
                {
                    "severity": "warning",
                    "text": "edge case",
                    "evidence": [{"node_id": "n1", "kind": "diff", "ref": "artifacts/diff.patch"}],
                }
            ],
        }
    )
    parse({"action": "finish", "summary": "done", "delivery_recommendation": "apply"})


BAD_RESPONSES: list[dict[str, Any]] = [
    {"action": "run_shell", "command": "rm -rf /"},
    {"action": "approve_plan", "revision": 1},
    {"action": "finish", "summary": "x", "permissions": "full"},  # unknown field
    {"action": "ask_user", "questions": []},
    {"action": "propose_plan", "explanation": "x", "graph": {"nodes": []}},
    {"action": "finish", "summary": "x", "delivery_recommendation": "merge"},
    {
        "action": "review",
        "summary": "x",
        "findings": [{"severity": "info", "text": "t", "evidence": []}],
    },
]


@pytest.mark.parametrize("response", BAD_RESPONSES)
def test_invalid_or_unknown_actions_are_rejected(response: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(response)


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "C:/Windows/x", "../x", "src/*.py", ".git/config", ".agents/runs/x/y", "src/"],
)
def test_context_paths_must_stay_inside_the_repository(path: str) -> None:
    with pytest.raises(ValidationError):
        parse(
            {
                "action": "request_context",
                "reason": "r",
                "selections": [{"kind": "repo", "path": path}],
            }
        )


def test_context_bounds_and_hash_shape() -> None:
    too_many = [{"kind": "repo", "path": f"f{i}.py"} for i in range(21)]
    with pytest.raises(ValidationError):
        parse({"action": "request_context", "reason": "r", "selections": too_many})
    with pytest.raises(ValidationError):
        parse(
            {
                "action": "request_context",
                "reason": "r",
                "selections": [{"kind": "repo", "path": "a", "lines": {"start": 5, "end": 2}}],
            }
        )
    with pytest.raises(ValidationError):
        parse(
            {
                "action": "request_context",
                "reason": "r",
                "selections": [
                    {
                        "kind": "artifact",
                        "run_id": "r",
                        "node_id": "n",
                        "attempt": 1,
                        "artifact": "a.txt",
                        "sha256": "XYZ",
                    }
                ],
            }
        )


def test_duplicate_question_ids_rejected() -> None:
    with pytest.raises(ValidationError):
        parse(
            {
                "action": "ask_user",
                "questions": [{"id": "q", "text": "a"}, {"id": "q", "text": "b"}],
            }
        )


def test_newer_spec_version_rejected() -> None:
    with pytest.raises(ValidationError):
        CoordinatorResponse.model_validate(
            {"spec_version": 99, "response": {"action": "finish", "summary": "x"}}
        )


def test_schema_is_exported_with_an_object_root() -> None:
    assert SCHEMAS["coordinator-response"] is CoordinatorResponse
    schema = schema_for(CoordinatorResponse)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    parsed = parse_spec(
        CoordinatorResponse, '{"response": {"action": "finish", "summary": "done"}}'
    )
    assert parsed.response.action == "finish"


# --- limits --------------------------------------------------------------------------------


def test_limit_defaults_match_the_design() -> None:
    limits = CoordinationLimits()
    assert (limits.max_steps, limits.max_duration_s) == (10, 300)
    assert (limits.context_max_bytes, limits.context_max_estimated_tokens) == (None, None)


def test_config_coordination_defaults_match_the_limits(tmp_path: Any) -> None:
    from dataclasses import asdict

    from relay.core.registry import load_config

    config = load_config(tmp_path, user_config=None).config
    assert asdict(CoordinationLimits()) == config.coordination.model_dump()


def test_project_config_overrides_coordination_limits(tmp_path: Any) -> None:
    from relay.core.registry import load_config

    agents = tmp_path / ".agents"
    agents.mkdir()
    (agents / "agents.yaml").write_text(
        "version: 1\ncoordination:\n  max_steps: 12\n", encoding="utf-8"
    )
    config = load_config(tmp_path, user_config=None).config
    assert config.coordination.max_steps == 12
    assert config.coordination.max_duration_s == 300


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_limits_must_be_positive_integers(bad: Any) -> None:
    with pytest.raises(ValueError):
        CoordinationLimits(max_steps=bad)
