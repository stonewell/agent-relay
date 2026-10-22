from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from design_blocks import blocks_in, design_blocks
from orch.core import specs
from orch.core.specs import (
    Graph,
    InputAnswers,
    ResultSpec,
    RouteEntry,
    TaskSpec,
    common,
    parse_spec,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def phase4(monkeypatch: pytest.MonkeyPatch) -> None:
    """The design describes the full system, including phase 4 cost fields."""
    monkeypatch.setattr(common, "CURRENT_PHASE", 4)


# --- the design's own examples validate -------------------------------------------------------


def test_design_code_blocks_parse() -> None:
    """AGENTS.md documentation rules: every yaml and json block in the design must parse."""
    for block in design_blocks():
        if block.lang == "json":
            json.loads(block.text)
        elif block.lang == "yaml":
            yaml.safe_load(block.text)


def test_design_task_spec_example(phase4: None) -> None:
    (block,) = blocks_in("5.2", "yaml")
    task = parse_spec(TaskSpec, block.text)
    assert task.id == "n3-implement-autosave"
    assert task.context.from_[0].node == "n2-research"
    assert task.routing.agent_rule == "route-impl-codex"


def test_design_result_spec_example() -> None:
    (block,) = blocks_in("5.3", "yaml")
    result = parse_spec(ResultSpec, block.text)
    assert result.status == "done"
    assert result.verification is not None
    assert result.verification.acceptance == "passed"


def test_design_input_answers_example() -> None:
    (block,) = blocks_in("5.5", "yaml")
    answers = parse_spec(InputAnswers, block.text)
    assert answers.submissions[0].provenance == "operator-tty"


def test_design_graph_example(phase4: None) -> None:
    (block,) = blocks_in("8.1", "yaml")
    graph = parse_spec(Graph, block.text)
    assert [n.id for n in graph.nodes][:2] == ["n1-research", "n2-impl-a"]


def test_design_route_entry_example() -> None:
    (block,) = blocks_in("4.4", "json")
    entry = parse_spec(RouteEntry, block.text)
    assert entry.event == "routed"
    assert entry.agent_rule == "route-impl-codex"
    dumped = entry.model_dump(by_alias=True, exclude_none=False)
    assert "from" in dumped


# --- invalid cases are rejected ---------------------------------------------------------------


def _task() -> dict[str, Any]:
    """The design example minus its phase 4 field, so it is valid in the current phase."""
    (block,) = blocks_in("5.2", "yaml")
    data = yaml.safe_load(block.text)
    del data["budget"]["max_cost_usd"]
    return data


def _result() -> dict[str, Any]:
    (block,) = blocks_in("5.3", "yaml")
    return yaml.safe_load(block.text)


def _route() -> dict[str, Any]:
    (block,) = blocks_in("4.4", "json")
    return json.loads(block.text)


def _mut(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(base)
    for key, value in changes.items():
        if value is ...:
            data.pop(key, None)
        else:
            data[key] = value
    return data


@pytest.mark.parametrize(
    "changes",
    [
        {"category": "poetry"},
        {"id": "../escape"},
        {"id": "CON"},
        {"id": "a" * 129},
        {"goal": ""},
        {"owns": [".git/hooks/*"]},
        {"owns": [".agents/runs/**"]},
        {"owns": [".agents/**"]},
        {"owns": ["/etc/passwd"]},
        {"owns": ["C:/Windows"]},
        {"owns": ["src/../../x"]},
        {"acceptance": [{"diff_scope": [".git/**"]}]},
        {"acceptance": [{"run": "make", "bogus": 1}]},
        {"verify": "none"},
        {"budget": {"max_cost_usd": 2.0}},
        {"permissions": "root"},
        {"spec_version": 2},
        {"spec_version": 0},
        {"context": {"from": [{"node": "n2"}]}},
        {"context": {"from": [{"node": "n2", "field": "summary", "artifact": "x"}]}},
        {"context": {"files": ["../outside"]}},
    ],
)
def test_task_spec_rejects(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(_mut(_task(), **changes))


def test_task_spec_accepts_whole_repo_and_agents_yaml_scopes() -> None:
    data = _mut(_task(), owns=["**", ".agents/agents.yaml", "src\\editor\\*.ts"])
    task = TaskSpec.model_validate(data)
    assert task.owns == ["**", ".agents/agents.yaml", "src/editor/*.ts"]


@pytest.mark.parametrize(
    "scope", [".agents/./runs/**", ".agents//runs/**", ".git/./hooks/*", ".agents\\.\\runs\\**"]
)
@pytest.mark.parametrize("field", ["owns", "diff_scope"])
def test_reserved_paths_reject_equivalent_spellings(scope: str, field: str) -> None:
    changes = {"owns": [scope]} if field == "owns" else {"acceptance": [{"diff_scope": [scope]}]}
    with pytest.raises(ValidationError, match="reserved runtime paths"):
        TaskSpec.model_validate(_mut(_task(), **changes))


@pytest.mark.parametrize(
    "scope",
    [
        ".GIT/**",
        ".AGENTS/runs/**",
        ".agents/RUNS/**",
        ".[G]IT/**",
        ".git./hooks/*",
        ".git /hooks/*",
        ".agents/runs./**",
        ".git::$INDEX_ALLOCATION/**",
        "GIT~1/hooks/*",
        "AGENTS~1/runs/**",
        ".agents/SHADOW~1/**",
        ".git/",
        ".agents/*/**",
        ".AGENTS/*/**",
        ".agents/*",
        "*/runs/**",
        "**/.git/**",
        "**/runs/**",
        "GIT~?/hooks/*",
        "GIT~[1]/hooks/*",
        "*~1/hooks/*",
        "AG1F2B~1/runs/**",
        ".[A-Z]it/**",
        ".git[.]/**",
        ".git[:]x/**",
    ],
)
@pytest.mark.parametrize("field", ["owns", "diff_scope"])
def test_reserved_paths_reject_case_and_windows_spellings(scope: str, field: str) -> None:
    changes = {"owns": [scope]} if field == "owns" else {"acceptance": [{"diff_scope": [scope]}]}
    with pytest.raises(ValidationError, match="reserved runtime paths"):
        TaskSpec.model_validate(_mut(_task(), **changes))


@pytest.mark.parametrize(
    "scope",
    [
        "gitx/**",
        "docs~old/**",
        "src/a~1.txt",
        ".github/**",
        ".agents/runsheet.md",
        ".agents/*.yaml",
        "**/*.py",
        "*/hooks/*",
    ],
)
def test_names_near_reserved_paths_stay_legal(scope: str) -> None:
    assert TaskSpec.model_validate(_mut(_task(), owns=[scope])).owns == [scope]


@pytest.mark.parametrize("scope", [".", "./", "././", "./.", ".//."])
def test_repository_root_spellings_are_rejected(scope: str) -> None:
    with pytest.raises(ValidationError, match="below the repository root"):
        TaskSpec.model_validate(_mut(_task(), owns=[scope]))


def test_scope_paths_canonicalize_segments() -> None:
    task = TaskSpec.model_validate(
        _mut(_task(), owns=["./src//./editor/*.ts", ".agents//./agents.yaml", "docs//"])
    )
    assert task.owns == ["src/editor/*.ts", ".agents/agents.yaml", "docs/"]


def test_verify_none_with_reason_is_allowed() -> None:
    task = TaskSpec.model_validate(
        _mut(_task(), verify="none", verification_skip_reason="docs only")
    )
    assert task.verify == "none"


def test_unknown_optional_fields_are_preserved() -> None:
    task = TaskSpec.model_validate(_mut(_task(), future_field={"a": 1}))
    assert task.model_dump()["future_field"] == {"a": 1}


def test_unknown_major_version_message() -> None:
    with pytest.raises(ValidationError, match="newer than the supported major version"):
        TaskSpec.model_validate(_mut(_task(), spec_version=2))


def test_design_examples_need_phase4_fields_rejected_in_phase1() -> None:
    (block,) = blocks_in("5.2", "yaml")
    with pytest.raises(ValidationError, match="available from phase 4"):
        parse_spec(TaskSpec, block.text)
    (graph,) = blocks_in("8.1", "yaml")
    with pytest.raises(ValidationError, match="available from phase 4"):
        parse_spec(Graph, graph.text)


def test_phase_gate_wording() -> None:
    with pytest.raises(ValidationError, match="available from phase 4"):
        TaskSpec.model_validate(_mut(_task(), budget={"max_cost_usd": 1}))


def test_phase_gate_lifts_in_later_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "CURRENT_PHASE", 4)
    task = TaskSpec.model_validate(_mut(_task(), budget={"max_cost_usd": 1}))
    assert task.budget.max_cost_usd == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "finished"},
        {"status": "needs_input"},
        {"status": "needs_input", "open_questions": [{"id": "q1", "text": "?", "kind": "choice"}]},
        {
            "status": "needs_input",
            "open_questions": [
                {"id": "q1", "text": "?", "kind": "choice", "options": ["a"], "default": "b"}
            ],
        },
        {
            "status": "needs_input",
            "open_questions": [{"id": "q1", "text": "?"}, {"id": "q1", "text": "again"}],
        },
        {"verification": {"acceptance": "skipped"}},
        {"verification": {"acceptance": "maybe"}},
    ],
)
def test_result_spec_rejects(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ResultSpec.model_validate(_mut(_result(), **changes))


def test_needs_input_defaults_question_revision() -> None:
    result = ResultSpec.model_validate(
        _mut(
            _result(),
            status="needs_input",
            open_questions=[{"id": "q1", "text": "Which cache?", "kind": "free", "required": True}],
        )
    )
    assert result.question_revision == 1


def test_done_result_keeps_advisory_questions() -> None:
    result = ResultSpec.model_validate(
        _mut(_result(), open_questions=[{"id": "q1", "text": "Check edge case?"}])
    )
    assert result.status == "done"


@pytest.mark.parametrize(
    "changes",
    [
        {"event": "rejected"},  # still has an agent and executed: true
        {"event": "previewed"},  # executed must be false
        {"event": "fallback"},  # needs "from"
        {"event": "teleported"},
        {"agent": None},  # routed without an agent
        {"seq": -1},
        {"permissions": "sudo"},
        {"category": "poetry"},
    ],
)
def test_route_entry_rejects(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RouteEntry.model_validate(_mut(_route(), **changes))


def test_route_entry_rejected_event_shape() -> None:
    entry = RouteEntry.model_validate(
        _mut(_route(), event="rejected", agent=None, executed=False, model=None)
    )
    assert entry.agent is None


def test_route_entry_round_trips_with_from_alias() -> None:
    data = _mut(
        _route(),
        event="fallback",
        **{"from": {"agent": "codex", "model": "m", "reason": "timeout"}},
    )
    entry = RouteEntry.model_validate(data)
    assert entry.model_dump(by_alias=True, mode="json")["from"]["reason"] == "timeout"


@pytest.mark.parametrize(
    "changes",
    [
        {"nodes": []},
        {"nodes": [{"id": "a", "category": "test", "goal": "x"}] * 2},
        {"policy": {"on_failure": "continue-all"}},
        {"policy": {"cost_mode": "advisory"}},
        {"policy": {"max_nodes": 1}},
    ],
)
def test_graph_rejects(changes: dict[str, Any]) -> None:
    (block,) = blocks_in("8.1", "yaml")
    base = yaml.safe_load(block.text)
    base["policy"] = {"on_failure": "continue-independent"}  # drops the phase 4 cost fields
    with pytest.raises(ValidationError):
        Graph.model_validate(_mut(base, **changes))


def test_input_answers_duplicate_submission_ids_rejected() -> None:
    (block,) = blocks_in("5.5", "yaml")
    data = yaml.safe_load(block.text)
    data["submissions"].append(copy.deepcopy(data["submissions"][0]))
    with pytest.raises(ValidationError, match="unique"):
        InputAnswers.model_validate(data)


def test_answers_hash_ignores_key_order_and_changes_with_content() -> None:
    (block,) = blocks_in("5.5", "yaml")
    sub = parse_spec(InputAnswers, block.text).submissions[0]
    reordered = sub.model_copy(update={"answers": dict(reversed(list(sub.answers.items())))})
    changed = sub.model_copy(update={"answers": {**sub.answers, "q1": "500ms"}})
    assert sub.content_hash() == reordered.content_hash()
    assert sub.content_hash() != changed.content_hash()


# --- parsing and generated schemas ------------------------------------------------------------


def test_yaml_dates_stay_strings() -> None:
    text = "spec_version: 1\nrun_id: 2026-10-22\nid: n1\ncategory: test\ngoal: g\n"
    assert parse_spec(TaskSpec, text).run_id == "2026-10-22"


def test_json_is_accepted_as_spec_text() -> None:
    text = json.dumps({"spec_version": 1, "id": "n1", "status": "done"})
    assert parse_spec(ResultSpec, text).id == "n1"


def test_committed_schemas_are_current(tmp_path: Path) -> None:
    """`schemas/*.json` is generated; regenerate with `python -m orch.core.specs`."""
    specs.export_schemas(tmp_path)
    for generated in tmp_path.glob("*.json"):
        committed = ROOT / "schemas" / generated.name
        assert committed.read_text(encoding="utf-8") == generated.read_text(encoding="utf-8"), (
            f"{committed.name} is stale"
        )
    assert {p.name for p in tmp_path.glob("*.json")} == {f"{n}.json" for n in specs.SCHEMAS}


def test_route_entry_schema_uses_from_not_from_underscore() -> None:
    props = specs.schema_for(RouteEntry)["properties"]
    assert "from" in props
    assert "from_" not in props
