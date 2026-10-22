"""Spike B findings as executable facts about the real CLIs (design sections 7.2 and 7.3).

The fixtures in `tests/fixtures/recorded/` are real outputs. If a CLI update changes a shape,
re-record them and the failing assertion names what drifted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from relay.core.specs import ResultSpec

DIR = Path(__file__).parent / "fixtures" / "recorded"


def claude(name: str) -> dict[str, Any]:
    return json.loads((DIR / name).read_text(encoding="utf-8"))


def codex_events(name: str) -> list[dict[str, Any]]:
    lines = (DIR / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def drop_nulls(value: Any) -> Any:
    """Codex's strict schemas make optional fields nullable; the adapter must drop the nulls."""
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [drop_nulls(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


# --- Claude -----------------------------------------------------------------------------------


def test_claude_json_envelope_has_session_id_result_and_usage() -> None:
    out = claude("claude_plain.json")
    assert out["type"] == "result"
    assert out["subtype"] == "success"
    assert out["is_error"] is False
    assert out["result"] == "OK"
    assert out["session_id"]
    assert out["total_cost_usd"] > 0
    assert {"input_tokens", "output_tokens"} <= out["usage"].keys()


def test_claude_usage_buckets_are_disjoint() -> None:
    """`input_tokens` excludes cache reads and writes, so cost needs no subtraction (8.5)."""
    usage = claude("claude_plain.json")["usage"]
    assert usage["input_tokens"] < usage["cache_read_input_tokens"]
    assert {"cache_creation_input_tokens", "cache_read_input_tokens"} <= usage.keys()


def test_claude_reports_the_model_that_actually_ran() -> None:
    """A request for `--model haiku` ran as another model in this environment, so the adapter
    must record the keys of `modelUsage`, not the model it asked for."""
    out = claude("claude_plain.json")
    assert list(out["modelUsage"]) == ["claude-sonnet-5-5"]


def test_claude_inline_json_schema_returns_structured_output() -> None:
    out = claude("claude_schema_done.json")
    assert out["structured_output"]["status"] == "done"
    assert (
        json.loads(out["result"]) == out["structured_output"]
    )  # `result` is the same JSON as text


def test_claude_needs_input_on_an_ambiguous_task_validates_as_a_result_spec() -> None:
    structured = claude("claude_schema_needs_input.json")["structured_output"]
    assert structured["status"] == "needs_input"
    result = ResultSpec.model_validate({"id": "n1", **structured})
    assert len(result.open_questions) >= 2
    assert {q.kind for q in result.open_questions} == {"free", "choice"}
    assert result.question_revision == 1


def test_claude_accepts_our_generated_result_schema_without_the_schema_key() -> None:
    out = claude("claude_result_spec_schema.json")
    assert out["is_error"] is False
    assert ResultSpec.model_validate(out["structured_output"]).status == "done"


def test_the_generated_schema_carries_a_schema_key_that_claude_rejects() -> None:
    """`claude --json-schema` fails with 'no schema with key or ref .../2020-12/schema', so the
    adapter strips `$schema` before passing the schema inline."""
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "result-spec.json").read_text(encoding="utf-8")
    )
    assert "$schema" in schema


def test_claude_resume_keeps_the_session_id_and_context() -> None:
    first = claude("claude_schema_done.json")
    resumed = claude("claude_resume.json")
    assert resumed["session_id"] == first["session_id"]
    assert resumed["result"] == "README.md"  # it remembered the earlier turn


# --- Codex ------------------------------------------------------------------------------------


def test_codex_jsonl_event_sequence_and_thread_id() -> None:
    events = codex_events("codex_plain.jsonl")
    assert [e["type"] for e in events] == [
        "thread.started",
        "turn.started",
        "item.completed",
        "turn.completed",
    ]
    assert events[0]["thread_id"]
    message = events[2]["item"]
    assert (message["type"], message["text"]) == ("agent_message", "OK")


def test_codex_input_tokens_include_cached_tokens() -> None:
    """Unlike Claude, `input_tokens` contains `cached_input_tokens`; subtract before pricing."""
    usage = codex_events("codex_plain.jsonl")[-1]["usage"]
    assert usage["cached_input_tokens"] <= usage["input_tokens"]
    assert usage["cached_input_tokens"] > 0
    assert "cost" not in json.dumps(usage)  # no price is reported, only tokens


def test_codex_resume_keeps_the_thread_id_and_context() -> None:
    first = codex_events("codex_plain.jsonl")[0]["thread_id"]
    resumed = codex_events("codex_resume.jsonl")
    assert resumed[0]["thread_id"] == first
    assert resumed[2]["item"]["text"] == "OK"


def test_codex_rejects_a_non_strict_output_schema_before_generating() -> None:
    events = codex_events("codex_schema_rejected.jsonl")
    assert [e["type"] for e in events] == ["thread.started", "turn.started", "error", "turn.failed"]
    message = json.loads(events[-1]["error"]["message"])["error"]
    assert message["code"] == "invalid_json_schema"
    assert "required to include every key in properties" in message["message"]


def test_codex_strict_schema_returns_needs_input_with_nulls_for_optional_fields() -> None:
    last = json.loads(
        (DIR / "codex_schema_needs_input_last_message.txt").read_text(encoding="utf-8")
    )
    assert last["status"] == "needs_input"
    assert last["open_questions"][0]["options"] is None  # nullable optional field
    result = ResultSpec.model_validate({"id": "n1", **drop_nulls(last)})
    assert result.status == "needs_input"
    assert len(result.open_questions) >= 2


def test_codex_final_message_in_the_event_stream_matches_the_output_file() -> None:
    events = codex_events("codex_schema_needs_input.jsonl")
    final = [
        e for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"
    ]
    in_stream = json.loads(final[-1]["item"]["text"])
    in_file = json.loads(
        (DIR / "codex_schema_needs_input_last_message.txt").read_text(encoding="utf-8")
    )
    assert in_stream == in_file


@pytest.mark.parametrize(
    "name",
    ["codex_plain.jsonl", "codex_resume.jsonl", "codex_schema_needs_input.jsonl"],
)
def test_codex_runs_end_with_turn_completed_and_usage(name: str) -> None:
    last = codex_events(name)[-1]
    assert last["type"] == "turn.completed"
    assert last["usage"]["output_tokens"] > 0


# --- Cursor -----------------------------------------------------------------------------------


def test_cursor_json_envelope_has_session_id_and_camel_case_usage() -> None:
    out = claude("cursor_plain.json")
    assert (out["type"], out["subtype"], out["is_error"], out["result"]) == (
        "result",
        "success",
        False,
        "OK",
    )
    assert out["session_id"]
    assert set(out["usage"]) == {
        "inputTokens",
        "outputTokens",
        "cacheReadTokens",
        "cacheWriteTokens",
    }
    assert "total_cost_usd" not in out  # tokens only; cost must be computed from pricing


def test_cursor_resume_keeps_the_session_id_and_context() -> None:
    assert claude("cursor_resume.json")["session_id"] == claude("cursor_plain.json")["session_id"]
    assert claude("cursor_resume.json")["result"] == "OK"


def test_cursor_needs_input_comes_back_as_json_text_needing_normalization() -> None:
    """No schema flag exists, so the reply is JSON text in `result`. Cursor emits `options: []`
    for free questions and `default: null`; the parser must normalise both before validating."""
    text = claude("cursor_prompt_needs_input.json")["result"]
    structured = json.loads(text)
    assert structured["status"] == "needs_input"
    with pytest.raises(ValueError):
        ResultSpec.model_validate({"id": "n1", **drop_nulls(structured)})  # empty options on free
    for question in structured["open_questions"]:
        if question["kind"] == "free" and not question["options"]:
            del question["options"]
    result = ResultSpec.model_validate({"id": "n1", **drop_nulls(structured)})
    assert [q.kind for q in result.open_questions] == ["free", "choice", "free"]
