"""`relay routes` and `relay explain` (design sections 4.4, 11 and 14)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from relay.cli.exitcodes import Exit
from relay.cli.main import main
from relay.core.events import EventLog
from relay.core.registry import RouteLogConfig
from relay.core.route_log import RouteLog
from relay.core.router import AgentDecision
from relay.core.specs.route import RouteEntry

RUN = "2026-10-22-a1b2"
OTHER = "2026-10-22-c3d4"
CLOCK = datetime(2026, 10, 22, 9, 41, 3, 123000, tzinfo=UTC)


def entry(
    *,
    run_id: str = RUN,
    seq: int,
    node_id: str = "n1",
    attempt: int = 1,
    event: str = "routed",
    ts: str = "2026-10-22T09:41:03.000Z",
    category: str = "implement",
    complexity: str = "high",
    agent: str | None = "codex",
    model: str | None = "codex-deep",
    effort: str | None = "high",
    agent_rule: str = "route-impl-codex",
    model_rule: str | None = "codex-deep-hard",
    reason: str | None = "category=implement matched route-impl-codex",
    title: str | None = "Add autosave",
    alternatives: tuple[dict[str, Any], ...] = (),
    from_: dict[str, Any] | None = None,
    permissions: str = "full",
    executed: bool = True,
    resumed: bool = False,
) -> RouteEntry:
    return RouteEntry.model_validate(
        {
            "ts": ts,
            "run_id": run_id,
            "seq": seq,
            "node_id": node_id,
            "attempt": attempt,
            "event": event,
            "title": title,
            "category": category,
            "complexity": complexity,
            "agent": agent,
            "model": model,
            "effort": effort,
            "agent_rule": agent_rule,
            "model_rule": model_rule,
            "reason": reason,
            "alternatives": list(alternatives),
            "from": from_,
            "permissions": permissions,
            "executed": executed,
            "resumed": resumed,
        }
    )


def add_run(tmp_path: Path, run_id: str, entries: list[RouteEntry]) -> Path:
    """Write canonical `node.routed` events only; no projections exist."""
    directory = tmp_path / ".agents" / "runs" / run_id
    directory.mkdir(parents=True)
    log = EventLog(directory / "events.jsonl")
    try:
        for item in entries:
            log.append(
                "node.routed",
                {
                    "node_id": item.node_id,
                    "route_seq": item.seq,
                    "decision": item.model_dump(mode="json", by_alias=True),
                },
            )
    finally:
        log.close()
    return directory


def call(tmp_path: Path, *args: str) -> int:
    return main(["-C", str(tmp_path), *args])


def payload(capsys: pytest.CaptureFixture[str]) -> Any:
    out = capsys.readouterr().out
    return json.loads(out)


# --- routes: reading and ordering -------------------------------------------------------------


def test_routes_aggregates_all_runs_oldest_first_and_stably(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_run(
        tmp_path,
        RUN,
        [entry(seq=1, run_id=RUN, ts="2026-10-22T09:41:03.000Z", node_id="n1")],
    )
    add_run(
        tmp_path,
        OTHER,
        [entry(seq=1, run_id=OTHER, ts="2026-10-22T09:41:03.000Z", node_id="n2")],
    )
    assert call(tmp_path, "routes") == Exit.OK
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if line.startswith("2026-10-22T")]
    assert len(rows) == 2
    # Same timestamp: run id breaks the tie, and an older entry comes first.
    assert rows[0].split()[1] == RUN
    assert rows[1].split()[1] == OTHER

    assert call(tmp_path, "routes", "--json") == Exit.OK
    data = payload(capsys)
    assert data["count"] == 2
    assert [item["run_id"] for item in data["entries"]] == [RUN, OTHER]


def test_routes_with_one_run_reads_only_that_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_run(tmp_path, RUN, [entry(seq=1, run_id=RUN, node_id="n1")])
    add_run(tmp_path, OTHER, [entry(seq=1, run_id=OTHER, node_id="n2")])
    assert call(tmp_path, "routes", RUN, "--json") == Exit.OK
    data = payload(capsys)
    assert data["count"] == 1
    assert data["entries"][0]["run_id"] == RUN


def test_filters_apply_across_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_run(tmp_path, RUN, [entry(seq=1, run_id=RUN, node_id="n1", agent="codex")])
    add_run(tmp_path, OTHER, [entry(seq=1, run_id=OTHER, node_id="n2", agent="claude")])
    assert call(tmp_path, "routes", "--agent", "claude", "--json") == Exit.OK
    data = payload(capsys)
    assert data["count"] == 1
    assert data["entries"][0]["run_id"] == OTHER


def test_routes_empty_store_is_not_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert call(tmp_path, "routes") == Exit.OK
    assert "no routing decisions" in capsys.readouterr().out
    assert call(tmp_path, "routes", "--json") == Exit.OK
    assert payload(capsys) == {"count": 0, "entries": []}


# --- routes: filters --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("--node", "n2"), [2]),
        (("--agent", "claude"), [2]),
        (("--model", "codex-fast"), [3]),
        (("--event", "escalated"), [3]),
        (("--category", "bugfix"), [2]),
        (("--rule", "route-bugfix-claude"), [2]),
        (("--rule", "codex-deep-hard"), [1]),
        (("--agent", "codex", "--event", "escalated"), [3]),
        (("--agent", "codex", "--event", "fallback"), []),
    ],
)
def test_each_filter_and_a_combination(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
    expected: list[int],
) -> None:
    add_run(
        tmp_path,
        RUN,
        [
            entry(
                seq=1,
                ts="2026-10-22T09:41:01.000Z",
                node_id="n1",
                agent="codex",
                model="codex-deep",
                category="implement",
                event="routed",
                agent_rule="route-impl-codex",
                model_rule="codex-deep-hard",
            ),
            entry(
                seq=2,
                ts="2026-10-22T09:41:02.000Z",
                node_id="n2",
                agent="claude",
                model="claude-deep",
                category="bugfix",
                event="fallback",
                agent_rule="route-bugfix-claude",
                model_rule="claude-impl",
                from_={"agent": "codex", "model": "codex-deep", "reason": "timeout"},
            ),
            entry(
                seq=3,
                ts="2026-10-22T09:41:03.000Z",
                node_id="n3",
                agent="codex",
                model="codex-fast",
                category="test",
                event="escalated",
                agent_rule="escalation",
                model_rule="escalation",
                from_={"agent": "codex", "model": "codex-deep", "reason": "acceptance failed"},
            ),
        ],
    )
    assert call(tmp_path, "routes", "--json", *args) == Exit.OK
    data = payload(capsys)
    assert [item["seq"] for item in data["entries"]] == expected


# --- routes: tail and order -------------------------------------------------------------------


def test_tail_and_order_are_stable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_run(
        tmp_path,
        RUN,
        [
            entry(seq=number, ts=f"2026-10-22T09:41:0{number}.000Z", node_id=f"n{number}")
            for number in range(1, 6)
        ],
    )
    assert call(tmp_path, "routes", "--tail", "2", "--json") == Exit.OK
    assert [item["seq"] for item in payload(capsys)["entries"]] == [4, 5]

    assert call(tmp_path, "routes", "--tail", "2", "--order", "newest", "--json") == Exit.OK
    assert [item["seq"] for item in payload(capsys)["entries"]] == [5, 4]

    assert call(tmp_path, "routes", "--order", "newest", "--json") == Exit.OK
    assert [item["seq"] for item in payload(capsys)["entries"]] == [5, 4, 3, 2, 1]


def test_negative_tail_is_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert call(tmp_path, "routes", "--tail", "-1") == Exit.INVALID
    assert "--tail" in capsys.readouterr().err


# --- routes: JSON schema ----------------------------------------------------------------------


def test_routes_json_entries_keep_the_route_entry_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_run(
        tmp_path,
        RUN,
        [
            entry(
                seq=1,
                alternatives=({"agent": "claude", "score": 1.5},),
            )
        ],
    )
    assert call(tmp_path, "routes", "--json") == Exit.OK
    data = payload(capsys)
    assert set(data) == {"count", "entries"}
    item = data["entries"][0]
    assert item["run_id"] == RUN
    assert item["seq"] == 1
    assert item["node_id"] == "n1"
    assert item["alternatives"] == [{"agent": "claude", "score": 1.5}]
    assert item["from"] is None


# --- explain ----------------------------------------------------------------------------------


def test_explain_shows_rules_alternatives_and_the_from_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_run(
        tmp_path,
        RUN,
        [
            entry(
                seq=1,
                attempt=1,
                node_id="n2-impl-a",
                event="routed",
                agent="codex",
                model="codex-deep",
                agent_rule="route-impl-codex",
                model_rule="codex-deep-hard",
            ),
            entry(
                seq=2,
                attempt=2,
                node_id="n2-impl-a",
                event="escalated",
                agent="codex",
                model="codex-huge",
                effort=None,
                agent_rule="escalation",
                model_rule="escalation",
                from_={"agent": "codex", "model": "codex-deep", "reason": "acceptance failed"},
            ),
            entry(
                seq=3,
                attempt=3,
                node_id="n2-impl-a",
                event="fallback",
                agent="claude",
                model="claude-deep",
                effort=None,
                agent_rule="fallback",
                model_rule="claude-impl",
                alternatives=({"agent": "codex", "score": 1.5},),
                from_={"agent": "codex", "model": "codex-huge", "reason": "timeout"},
            ),
        ],
    )
    assert call(tmp_path, "explain", RUN, "n2-impl-a") == Exit.OK
    out = capsys.readouterr().out
    assert "latest: attempt 3" in out
    assert "agent_rule=fallback" in out
    assert "model_rule=claude-impl" in out
    assert "from: codex/codex-huge (timeout)" in out
    assert "alternatives: codex 1.5" in out
    assert "history (oldest first):" in out
    assert "seq 1 attempt 1 routed" in out
    assert "seq 2 attempt 2 escalated" in out

    assert call(tmp_path, "explain", RUN, "n2-impl-a", "--json") == Exit.OK
    data = payload(capsys)
    assert data["current"]["event"] == "fallback"
    assert data["current"]["agent_rule"] == "fallback"
    assert data["current"]["model_rule"] == "claude-impl"
    assert data["alternatives"] == [{"agent": "codex", "score": 1.5}]
    assert [item["event"] for item in data["chain"]] == ["routed", "escalated", "fallback"]
    assert data["chain"][2]["from"] == {
        "agent": "codex",
        "model": "codex-huge",
        "reason": "timeout",
    }


def test_explain_reads_busy_and_exhausted_from_the_structured_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_run(
        tmp_path,
        RUN,
        [
            entry(
                seq=1,
                title=None,
                reason="agent_rule=route-impl-codex;model_rule=codex-deep-hard;queued;exhausted=gemini,claude",
                alternatives=({"agent": "claude", "score": 0.7},),
            )
        ],
    )
    assert call(tmp_path, "explain", RUN, "n1") == Exit.OK
    out = capsys.readouterr().out
    assert "queued (waiting for capacity)" in out
    assert "exhausted: gemini, claude" in out
    assert "task: -" in out

    assert call(tmp_path, "explain", RUN, "n1", "--json") == Exit.OK
    signals = payload(capsys)["signals"]
    assert signals == {
        "queued": True,
        "rejected": False,
        "exhausted": ["gemini", "claude"],
        "model_unavailable": False,
        "structured": True,
    }


def test_explain_reports_a_rejected_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_run(
        tmp_path,
        RUN,
        [
            entry(
                seq=1,
                agent=None,
                model=None,
                effort=None,
                event="rejected",
                agent_rule="score",
                model_rule=None,
                title=None,
                reason="agent_rule=score;rejected",
                executed=False,
            )
        ],
    )
    assert call(tmp_path, "explain", RUN, "n1", "--json") == Exit.OK
    data = payload(capsys)
    assert data["current"]["agent"] is None
    assert data["signals"]["rejected"] is True


# --- canonical events survive missing projections and a torn tail -----------------------------


def test_explain_works_when_the_projections_are_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / ".agents" / "runs" / RUN
    directory.mkdir(parents=True)
    events = EventLog(directory / "events.jsonl")
    log = RouteLog(
        events,
        directory / "routes.jsonl",
        directory / "route.log",
        RouteLogConfig(),
        run_id=RUN,
        now=lambda: CLOCK,
    )
    log.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=AgentDecision(
            agent="codex", agent_rule="score", reason="score picked codex"
        ),
        goal="Add autosave",
    )
    log.close()
    assert (directory / "routes.jsonl").is_file()
    (directory / "routes.jsonl").unlink()
    (directory / "route.log").unlink()

    assert call(tmp_path, "routes", "--json") == Exit.OK
    assert payload(capsys)["count"] == 1
    assert call(tmp_path, "explain", RUN, "n1") == Exit.OK
    assert "agent_rule=score" in capsys.readouterr().out


def test_torn_trailing_record_is_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = add_run(tmp_path, RUN, [entry(seq=1)])
    events_path = directory / "events.jsonl"
    with open(events_path, "ab") as handle:
        handle.write(b'{"seq":2,"ts":"2026-10-22T09:4')
    assert call(tmp_path, "routes", "--json") == Exit.OK
    assert payload(capsys)["count"] == 1
    assert call(tmp_path, "explain", RUN, "n1") == Exit.OK


# --- errors -----------------------------------------------------------------------------------


def test_unknown_run_is_a_clean_invalid_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert call(tmp_path, "routes", "2026-10-22-nope") == Exit.INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "run '2026-10-22-nope' not found" in captured.err
    assert "Traceback" not in captured.err

    assert call(tmp_path, "routes", "2026-10-22-nope", "--json") == Exit.INVALID
    captured = capsys.readouterr()
    assert json.loads(captured.err)["error"].startswith("relay routes: run")


def test_unknown_node_is_a_clean_invalid_error_with_a_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_run(tmp_path, RUN, [entry(seq=1, node_id="n1")])
    assert call(tmp_path, "explain", RUN, "n-missing") == Exit.INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "node 'n-missing' has no routing decisions" in captured.err
    assert f"relay routes {RUN} --node n-missing" in captured.err
    assert "Traceback" not in captured.err


def test_unknown_run_for_explain_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert call(tmp_path, "explain", "2026-10-22-nope", "n1") == Exit.INVALID
    assert "not found" in capsys.readouterr().err


# --- privacy: include_goal false --------------------------------------------------------------


def test_include_goal_false_never_prints_titles_or_free_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / ".agents" / "runs" / RUN
    directory.mkdir(parents=True)
    events = EventLog(directory / "events.jsonl")
    log = RouteLog(
        events,
        directory / "routes.jsonl",
        directory / "route.log",
        RouteLogConfig(include_goal=False),
        run_id=RUN,
        now=lambda: CLOCK,
    )
    log.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="high",
        agent_decision=AgentDecision(
            agent="codex", agent_rule="route-impl-codex", reason="secret free text"
        ),
        model_decision=None,
        goal="Debounce the secret autosave handler",
    )
    log.close()

    assert call(tmp_path, "explain", RUN, "n1") == Exit.OK
    out = capsys.readouterr().out
    assert "Debounce" not in out
    assert "secret free text" not in out
    assert "task: -" in out
    assert "reason: agent_rule=route-impl-codex" in out

    assert call(tmp_path, "routes") == Exit.OK
    table = capsys.readouterr().out
    assert "Debounce" not in table
    assert "secret free text" not in table
