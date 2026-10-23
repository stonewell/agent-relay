"""Shared result parsing and validation (task 1.11, design sections 5.4 and 7.3).

Deterministic tests of the one pipeline every adapter delegates to: the four-step parsing
order, schema generation, normalisation, validation, and the single schema-repair follow-up.
No network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from relay.adapters.base import RESULT_FILE, RunContext
from relay.adapters.parsing import (
    ABSENT,
    RepairOutcome,
    ResultInvalid,
    claude_schema,
    node_file_source,
    normalize_result,
    parse_sources,
    parse_with_repair,
    stdout_block_source,
    strict_schema,
    strip_operational_fields,
    validate_result,
)
from relay.core.specs import ResultSpec, schema_for

DIR = Path(__file__).parent / "fixtures" / "recorded"


def ctx(tmp_path: Path) -> RunContext:
    node_dir = tmp_path / "node"
    node_dir.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return RunContext(
        run_id="2026-10-22-test",
        node_id="n1",
        attempt=1,
        node_dir=node_dir,
        worktree=worktree,
    )


def _finalize(candidate: object) -> ResultSpec:
    return validate_result(candidate, node_id="n1", agent="test")


# --- schema generation (section 5.4) -----------------------------------------------------------


def test_claude_schema_strips_unsupported_metadata() -> None:
    schema = claude_schema()
    assert "$schema" not in schema
    assert "title" not in schema
    assert schema["properties"].keys() == schema_for(ResultSpec)["properties"].keys()
    assert schema["type"] == "object"


def test_strict_schema_every_object_is_strict_and_drops_meta() -> None:
    strict = strict_schema(schema_for(ResultSpec))
    assert "$schema" not in strict
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == set(strict["properties"])
    # A nested definition is strict too.
    assert strict["$defs"]["OpenQuestion"]["additionalProperties"] is False
    assert set(strict["$defs"]["OpenQuestion"]["required"]) == set(
        strict["$defs"]["OpenQuestion"]["properties"]
    )


# --- the four-step parsing order (section 7.3) -------------------------------------------------


def test_higher_priority_source_wins_and_lower_sources_never_run(tmp_path: Path) -> None:
    order: list[str] = []

    def native() -> object:
        order.append("native")
        return {"id": "n1", "status": "done", "summary": "native"}

    def node_file() -> object:
        order.append("node-file")
        raise AssertionError("node-file must not run")

    def block() -> object:
        order.append("block")
        raise AssertionError("block must not run")

    def synthesized() -> object:
        order.append("synthesized")
        raise AssertionError("synthesized must not run")

    result = parse_sources((native, node_file, block, synthesized), finalize=_finalize)
    assert result.summary == "native"
    assert order == ["native"]


def test_absent_sources_fall_through_to_the_next_step(tmp_path: Path) -> None:
    order: list[str] = []

    def native() -> object:
        order.append("native")
        return ABSENT

    def node_file() -> object:
        order.append("node-file")
        return ABSENT

    def block() -> object:
        order.append("block")
        return {"id": "n1", "status": "done", "summary": "block"}

    def synthesized() -> object:
        order.append("synthesized")
        raise AssertionError("synthesized must not run")

    result = parse_sources((native, node_file, block, synthesized), finalize=_finalize)
    assert result.summary == "block"
    assert order == ["native", "node-file", "block"]


def test_synthesized_candidate_is_the_final_fallback(tmp_path: Path) -> None:
    result = parse_sources(
        (lambda: ABSENT, lambda: ABSENT, lambda: ABSENT, lambda: {"id": "n1", "status": "done"}),
        finalize=_finalize,
    )
    assert result.status == "done"


def test_node_file_source_reads_yaml_and_is_absent_when_missing(tmp_path: Path) -> None:
    run_ctx = ctx(tmp_path)
    assert node_file_source(run_ctx) is ABSENT
    (run_ctx.node_dir / RESULT_FILE).write_text("id: n1\nstatus: done\nsummary: from-file\n")
    assert node_file_source(run_ctx) == {"id": "n1", "status": "done", "summary": "from-file"}


def test_stdout_block_source_extracts_fenced_json_and_yaml() -> None:
    assert stdout_block_source('prose\n```json\n{"id": "n1", "status": "done"}\n```\n') == {
        "id": "n1",
        "status": "done",
    }
    assert stdout_block_source("```yaml\nid: n1\nstatus: done\n```\n") == {
        "id": "n1",
        "status": "done",
    }
    assert stdout_block_source("no block here") is ABSENT


@pytest.mark.parametrize(
    "content",
    ["id: [unclosed", "id: n1", "id: n1\nid: n2\n"],
    ids=["yaml-syntax", "schema", "duplicate-key"],
)
def test_malformed_or_unreadable_sources_raise_result_invalid(tmp_path: Path, content: str) -> None:
    run_ctx = ctx(tmp_path)
    (run_ctx.node_dir / RESULT_FILE).write_text(content, encoding="utf-8")
    with pytest.raises(ResultInvalid):
        parse_sources(
            (lambda: ABSENT, lambda: node_file_source(run_ctx), lambda: ABSENT, lambda: ABSENT),
            finalize=_finalize,
        )


def test_unreadable_utf16_node_file_raises_result_invalid(tmp_path: Path) -> None:
    run_ctx = ctx(tmp_path)
    (run_ctx.node_dir / RESULT_FILE).write_bytes("id: n1".encode("utf-16"))
    with pytest.raises(ResultInvalid):
        parse_sources(
            (lambda: ABSENT, lambda: node_file_source(run_ctx), lambda: ABSENT, lambda: ABSENT),
            finalize=_finalize,
        )


# --- normalisation and validation (section 5.4) ------------------------------------------------


def test_normalize_drops_nulls_and_empty_options_on_free_questions() -> None:
    data: dict[str, Any] = {
        "id": "n1",
        "status": "needs_input",
        "summary": None,
        "open_questions": [
            {"id": "q1", "text": "free?", "kind": "free", "options": [], "default": None},
            {"id": "q2", "text": "choice?", "kind": "choice", "options": ["a"], "default": "a"},
        ],
    }
    normalized = normalize_result(data)
    assert "summary" not in normalized  # null dropped
    free = normalized["open_questions"][0]
    assert "options" not in free  # empty options on a free question removed
    assert "default" not in free  # null default dropped
    assert normalized["open_questions"][1]["options"] == ["a"]  # choice untouched


def test_validate_result_fills_id_and_agent_and_rejects_invalid() -> None:
    result = validate_result({"status": "done"}, node_id="n1", agent="claude")
    assert (result.id, result.agent, result.status) == ("n1", "claude", "done")
    with pytest.raises(ResultInvalid):
        validate_result({"status": "bogus"}, node_id="n1", agent="claude")
    with pytest.raises(ResultInvalid):
        validate_result("not an object", node_id="n1", agent="claude")
    with pytest.raises(ResultInvalid):
        validate_result(None, node_id="n1", agent="claude")


def test_validate_result_forces_id_and_agent_over_agent_declared_values() -> None:
    result = validate_result(
        {"id": "other-node", "agent": "cursor", "status": "done"},
        node_id="n1",
        agent="claude",
    )
    assert (result.id, result.agent) == ("n1", "claude")


def test_strip_operational_fields_removes_only_operational_keys() -> None:
    candidate: dict[str, Any] = {
        "id": "other-node",
        "agent": "cursor",
        "model": 123,
        "session": "evil",
        "usage": "evil",
        "status": "done",
        "summary": "kept",
    }
    stripped = strip_operational_fields(candidate)
    assert stripped == {"status": "done", "summary": "kept"}
    assert strip_operational_fields("not-an-object") == "not-an-object"


# --- the single schema-repair follow-up (sections 5.4 and 8.2) ---------------------------------


def test_no_failure_never_repairs() -> None:
    async def parse() -> ResultSpec:
        return ResultSpec(id="n1", status="done")

    async def repair(error: str) -> ResultSpec:
        raise AssertionError("repair must not run")

    outcome = asyncio.run(parse_with_repair(parse, repair, attempt=1))
    assert outcome.repaired is False
    assert outcome.result is not None
    assert outcome.attempt == 1


def test_first_failure_triggers_exactly_one_repair_and_succeeds() -> None:
    seen: list[str] = []

    async def parse() -> ResultSpec:
        seen.append("parse")
        raise ResultInvalid("bad result")

    async def repair(error: str) -> ResultSpec:
        seen.append(f"repair:{error}")
        return ResultSpec(id="n1", status="done", summary="fixed")

    outcome = asyncio.run(parse_with_repair(parse, repair, attempt=2))
    assert seen == ["parse", "repair:bad result"]
    assert outcome.repaired is True
    assert outcome.attempt == 2
    assert outcome.failed is False
    assert outcome.result is not None and outcome.result.summary == "fixed"


def test_second_failure_is_terminal_without_a_second_repair() -> None:
    repairs = 0

    async def parse() -> ResultSpec:
        raise ResultInvalid("first failure")

    async def repair(error: str) -> ResultSpec:
        nonlocal repairs
        repairs += 1
        raise ResultInvalid("second failure")

    outcome = asyncio.run(parse_with_repair(parse, repair, attempt=4))
    assert repairs == 1  # exactly one repair, never a second
    assert outcome.result is None
    assert outcome.failed is True
    assert outcome.repaired is True
    assert outcome.attempt == 4  # charged to the same attempt, never incremented
    assert "second failure" in (outcome.error or "")


def test_repair_outcome_helpers() -> None:
    ok = RepairOutcome(result=ResultSpec(id="n1", status="done"), attempt=3, repaired=True)
    assert ok.failed is False
    terminal = RepairOutcome(result=None, attempt=3, repaired=True, error="bad")
    assert terminal.failed is True


# --- golden fixtures through the shared module -------------------------------------------------


def test_claude_golden_structured_output_parses_through_the_shared_module() -> None:
    payload = json.loads((DIR / "claude_schema_done.json").read_text(encoding="utf-8"))
    result = validate_result(payload["structured_output"], node_id="n1", agent="claude")
    assert result.status == "done"


def test_codex_golden_last_message_parses_through_the_shared_module() -> None:
    last = json.loads(
        (DIR / "codex_schema_needs_input_last_message.txt").read_text(encoding="utf-8")
    )
    result = validate_result(last, node_id="n1", agent="codex")
    assert result.status == "needs_input"
    assert all(question.options is None for question in result.open_questions)


def test_cursor_golden_needs_normalization_before_validation() -> None:
    text = json.loads((DIR / "cursor_prompt_needs_input.json").read_text(encoding="utf-8"))[
        "result"
    ]
    structured = json.loads(text)
    # Cursor emits `options: []` and `default: null` on free questions, which the
    # OpenQuestion model rejects; normalisation removes them before validation.
    normalized = normalize_result(structured)
    assert [question["kind"] for question in normalized["open_questions"]] == [
        "free",
        "choice",
        "free",
    ]
    free = normalized["open_questions"][0]
    assert "options" not in free and "default" not in free
    result = validate_result(structured, node_id="n1", agent="cursor")
    assert result.status == "needs_input"
    assert all(q.options is None for q in result.open_questions if q.kind == "free")
