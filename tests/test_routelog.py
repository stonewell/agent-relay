"""Route log behaviour (design section 4.4)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from relay.core.events import CATALOG, EventLog, read_events
from relay.core.model_router import ModelDecision
from relay.core.registry import RouteLogConfig
from relay.core.route_log import (
    RouteLog,
    build_entry,
    derive_title,
    rebuild_projections,
    redact_secrets,
    structured_reason,
)
from relay.core.router import AgentDecision
from relay.core.specs.common import PermissionLevel
from relay.core.specs.route import Alternative, PreviousRoute, RouteEntry

CLOCK = datetime(2026, 10, 22, 9, 41, 3, 123000, tzinfo=UTC)
RUN_ID = "2026-10-22-a1b2"


def log_at(path: Path) -> EventLog:
    return EventLog(path, now=lambda: CLOCK)


def decision(
    *,
    agent: str | None = "codex",
    agent_rule: str = "route-impl-codex",
    reason: str = "category=implement matched route-impl-codex; codex preferred, load 0/3",
    alternatives: tuple[Alternative, ...] = (),
    rejected: bool = False,
    queued: bool = False,
    permissions: PermissionLevel = "full",
) -> AgentDecision:
    return AgentDecision(
        agent=agent,
        agent_rule=agent_rule,
        reason=reason,
        alternatives=alternatives,
        rejected=rejected,
        queued=queued,
        permissions=permissions,
    )


def model(
    *,
    model: str = "codex-deep",
    tier: str | None = "deep",
    effort: str | None = "high",
    model_rule: str = "codex-deep-hard",
    warning: str | None = None,
) -> ModelDecision:
    return ModelDecision(
        model=model, tier=tier, effort=effort, model_rule=model_rule, warning=warning
    )


def make(
    tmp_path: Path,
    *,
    enabled: bool = True,
    include_goal: bool = True,
    title_max_chars: int = 120,
) -> RouteLog:
    config = RouteLogConfig(
        enabled=enabled, include_goal=include_goal, title_max_chars=title_max_chars
    )
    events = log_at(tmp_path / "events.jsonl")
    return RouteLog(
        events,
        tmp_path / "routes.jsonl",
        tmp_path / "route.log",
        config,
        run_id=RUN_ID,
        now=lambda: CLOCK,
    )


def routed_events(path: Path) -> list[dict[str, Any]]:
    return [dict(e.data) for e in read_events(path) if e.type == "node.routed"]


def routes_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# --- durable before dispatch ------------------------------------------------------------------


def test_entry_is_written_before_dispatch_and_survives_a_hard_exit(tmp_path: Path) -> None:
    """The canonical event and both projections survive an immediate kill after `route`."""
    writer = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from datetime import datetime, UTC
        from relay.core.events import EventLog
        from relay.core.registry import RouteLogConfig
        from relay.core.router import AgentDecision
        from relay.core.route_log import RouteLog

        root = Path(sys.argv[1])
        clock = lambda: datetime(2026, 10, 22, 9, 41, 3, 123000, tzinfo=UTC)
        events = EventLog(root / "events.jsonl", now=clock)
        log = RouteLog(
            events,
            root / "routes.jsonl",
            root / "route.log",
            RouteLogConfig(),
            run_id="2026-10-22-a1b2",
            now=clock,
        )
        for i in range(5):
            log.route(
                node_id="n1",
                attempt=i + 1,
                event="routed",
                category="implement",
                complexity="low",
                agent_decision=AgentDecision(agent="codex", agent_rule="score", reason="r"),
            )
        print("done", flush=True)
        import os
        os._exit(0)  # no close: a crash right after the final route()
        """
    )
    subprocess.run([sys.executable, "-I", "-c", writer, str(tmp_path)], check=True)
    assert len(routed_events(tmp_path / "events.jsonl")) == 5
    assert len(routes_lines(tmp_path / "routes.jsonl")) == 5
    assert len(log_lines(tmp_path / "route.log")) == 5


def test_canonical_write_failure_prevents_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = make(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("disk gone")

    monkeypatch.setattr(EventLog, "append", boom)

    with pytest.raises(OSError, match="disk gone"):
        log.route(
            node_id="n1",
            attempt=1,
            event="routed",
            category="implement",
            complexity="low",
            agent_decision=decision(),
        )
    assert not (tmp_path / "routes.jsonl").exists()
    assert not (tmp_path / "route.log").exists()


# --- one entry per attempt, monotonic seq ----------------------------------------------------


def test_exactly_one_entry_per_attempt_and_monotonic_seq(tmp_path: Path) -> None:
    log = make(tmp_path)
    first = log.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=decision(),
        model_decision=model(),
    )
    second = log.route(
        node_id="n1",
        attempt=2,
        event="escalated",
        category="implement",
        complexity="low",
        agent_decision=decision(agent_rule="escalation"),
        model_decision=model(tier="deep", model_rule="escalation"),
        previous=PreviousRoute(agent="codex", model="codex-deep", reason="acceptance failed"),
    )
    assert (first.seq, second.seq) == (1, 2)
    assert log.next_seq == 3
    routes = routes_lines(tmp_path / "routes.jsonl")
    assert [entry["attempt"] for entry in routes] == [1, 2]
    assert [entry["seq"] for entry in routes] == [1, 2]
    assert len(routed_events(tmp_path / "events.jsonl")) == 2


def test_seq_continues_after_reopen(tmp_path: Path) -> None:
    first = make(tmp_path)
    first.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=decision(),
    )
    first.close()
    # A fresh writer (same run directory) picks the route seq up where the last one left it.
    reopened = make(tmp_path)
    assert reopened.next_seq == 2
    entry = reopened.route(
        node_id="n1",
        attempt=2,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=decision(),
    )
    assert entry.seq == 2


# --- event variants, `from`, implicit ids ----------------------------------------------------


def test_escalation_and_fallback_carry_event_from_and_rules(tmp_path: Path) -> None:
    log = make(tmp_path)
    log.route(
        node_id="n2-impl-a",
        attempt=2,
        event="fallback",
        category="implement",
        complexity="high",
        agent_decision=decision(agent="claude", agent_rule="fallback", reason="timeout"),
        model_decision=model(model="claude-deep", model_rule="claude-impl", effort=None),
        previous=PreviousRoute(agent="codex", model="codex-deep", reason="timeout"),
    )
    (entry,) = routes_lines(tmp_path / "routes.jsonl")
    assert entry["event"] == "fallback"
    assert entry["agent"] == "claude"
    assert entry["agent_rule"] == "fallback"
    assert entry["model_rule"] == "claude-impl"
    assert entry["from"] == {"agent": "codex", "model": "codex-deep", "reason": "timeout"}


def test_escalated_entry_has_model_rule_escalation_verbatim(tmp_path: Path) -> None:
    log = make(tmp_path)
    log.route(
        node_id="n1",
        attempt=2,
        event="escalated",
        category="bugfix",
        complexity="high",
        agent_decision=decision(agent_rule="escalation"),
        model_decision=model(model_rule="escalation"),
        previous=PreviousRoute(agent="codex", model="codex-deep"),
    )
    (entry,) = routes_lines(tmp_path / "routes.jsonl")
    assert entry["event"] == "escalated"
    assert entry["model_rule"] == "escalation"
    assert entry["agent_rule"] == "escalation"


@pytest.mark.parametrize("rule", ["pinned", "category-match", "score", "llm-classifier"])
def test_implicit_rule_ids_are_stored_verbatim(tmp_path: Path, rule: str) -> None:
    log = make(tmp_path)
    log.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=decision(agent_rule=rule),
        model_decision=model(model_rule="default"),
    )
    (entry,) = routes_lines(tmp_path / "routes.jsonl")
    assert entry["agent_rule"] == rule
    assert entry["model_rule"] == "default"


def test_previewed_entry_has_executed_false(tmp_path: Path) -> None:
    log = make(tmp_path)
    log.route(
        node_id="n1",
        attempt=1,
        event="previewed",
        category="implement",
        complexity="low",
        agent_decision=decision(),
        model_decision=model(),
        executed=False,
    )
    (entry,) = routes_lines(tmp_path / "routes.jsonl")
    assert entry["event"] == "previewed"
    assert entry["executed"] is False


def test_rejected_entry_has_no_agent_and_no_execution(tmp_path: Path) -> None:
    log = make(tmp_path)
    log.route(
        node_id="n1",
        attempt=1,
        event="rejected",
        category="implement",
        complexity="low",
        agent_decision=decision(agent=None, agent_rule="score", rejected=True),
        executed=False,
    )
    (entry,) = routes_lines(tmp_path / "routes.jsonl")
    assert entry["event"] == "rejected"
    assert entry["agent"] is None
    assert entry["executed"] is False
    assert entry["model"] is None


def test_rerouted_on_resume_sets_resumed_and_requires_from(tmp_path: Path) -> None:
    log = make(tmp_path)
    log.route(
        node_id="n1",
        attempt=1,
        event="rerouted",
        category="implement",
        complexity="low",
        agent_decision=decision(
            agent="claude", agent_rule="pinned", reason="operator changed agent"
        ),
        model_decision=model(model="claude-deep", model_rule="claude-impl", effort=None),
        previous=PreviousRoute(agent="codex", model="codex-deep", reason="inputs changed"),
        resumed=True,
    )
    (entry,) = routes_lines(tmp_path / "routes.jsonl")
    assert entry["event"] == "rerouted"
    assert entry["resumed"] is True
    assert entry["from"] == {
        "agent": "codex",
        "model": "codex-deep",
        "reason": "inputs changed",
    }


def test_rerouted_without_previous_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires 'from'"):
        build_entry(
            run_id=RUN_ID,
            node_id="n1",
            attempt=1,
            event="rerouted",
            category="implement",
            complexity="low",
            agent_decision=decision(),
            model_decision=model(),
        )


# --- title derivation ------------------------------------------------------------------------


def test_explicit_title_wins_over_goal() -> None:
    assert derive_title("Do the thing", "Some other goal.", 120) == "Do the thing"


def test_title_falls_back_to_first_sentence_of_goal() -> None:
    assert derive_title(None, "Add debounce. Then test it.", 120) == "Add debounce"


def test_title_whitespace_is_collapsed() -> None:
    assert derive_title(None, "Add\na   debounce\n\nhandler.", 120) == "Add a debounce handler"


def test_title_is_truncated_to_max_chars() -> None:
    assert derive_title(None, "x" * 500, 40) == "x" * 40


def test_empty_title_yields_none() -> None:
    assert derive_title("   ", "", 120) is None


# --- secret redaction ------------------------------------------------------------------------


def test_secret_patterns_are_redacted_in_titles_and_reasons(tmp_path: Path) -> None:
    log = make(tmp_path)
    log.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=decision(reason="matched rule; token=supersecretvalue leaked"),
        goal="Rotate the sk-abcdefghijklmnop key",
    )
    (entry,) = routes_lines(tmp_path / "routes.jsonl")
    assert "supersecretvalue" not in entry["title"]
    assert "sk-abcdefghijklmnop" not in entry["title"]
    assert "<redacted>" in entry["title"]
    assert "supersecretvalue" not in entry["reason"]
    assert "<redacted>" in entry["reason"]


def test_redact_secrets_masks_url_credentials_and_keys() -> None:
    assert redact_secrets("use https://user:pass@example.invalid now") == (
        "use https://<redacted>@example.invalid now"
    )
    assert redact_secrets("set password=opensesame here") == "set password=<redacted> here"
    assert redact_secrets("AKIA1234567890ABCDEF is the key") == "<redacted> is the key"


# --- include_goal: false ---------------------------------------------------------------------


def test_include_goal_false_omits_titles_and_free_text_reasons(tmp_path: Path) -> None:
    log = make(tmp_path, include_goal=False)
    log.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="high",
        agent_decision=decision(
            reason="category=implement matched route-impl-codex; codex preferred, load 0/3",
            alternatives=(Alternative(agent="claude", score=7.9),),
        ),
        model_decision=model(),
        goal="Debounce the autosave handler",
    )
    # Canonical event decision, routes.jsonl and route.log all drop the title and free text.
    (canonical,) = routed_events(tmp_path / "events.jsonl")
    canonical_decision = canonical["decision"]
    (projection,) = routes_lines(tmp_path / "routes.jsonl")

    for entry in (canonical_decision, projection):
        assert entry["title"] is None
        assert entry["reason"] == "agent_rule=route-impl-codex;model_rule=codex-deep-hard"
        assert entry["agent"] == "codex"
        assert entry["model"] == "codex-deep"
        assert entry["effort"] == "high"
        assert entry["agent_rule"] == "route-impl-codex"
        assert entry["model_rule"] == "codex-deep-hard"
        assert entry["alternatives"] == [{"agent": "claude", "score": 7.9}]
        assert entry["permissions"] == "full"

    (line,) = log_lines(tmp_path / "route.log")
    assert "Debounce" not in line
    assert "agent_rule=route-impl-codex" in line
    assert "codex/codex-deep(high)" in line


def test_include_goal_false_drops_from_reason_but_keeps_identity(tmp_path: Path) -> None:
    log = make(tmp_path, include_goal=False)
    log.route(
        node_id="n1",
        attempt=2,
        event="fallback",
        category="implement",
        complexity="low",
        agent_decision=decision(agent="claude", agent_rule="fallback", reason="timeout"),
        model_decision=model(model="claude-deep", model_rule="claude-impl", effort=None),
        previous=PreviousRoute(agent="codex", model="codex-deep", reason="timeout after 30s"),
    )
    (entry,) = routes_lines(tmp_path / "routes.jsonl")
    assert entry["from"] == {"agent": "codex", "model": "codex-deep", "reason": None}
    (line,) = log_lines(tmp_path / "route.log")
    assert "(from codex)" in line
    assert "timeout after 30s" not in line


def test_structured_reason_codes() -> None:
    assert structured_reason(decision(), model()) == (
        "agent_rule=route-impl-codex;model_rule=codex-deep-hard"
    )
    assert structured_reason(decision(agent=None, rejected=True), None) == (
        "agent_rule=route-impl-codex;rejected"
    )
    assert structured_reason(decision(queued=True), model(warning="model unavailable")) == (
        "agent_rule=route-impl-codex;model_rule=codex-deep-hard;queued;model=unavailable"
    )


# --- projection failure and rebuild ----------------------------------------------------------


def test_projection_write_failure_warns_without_failing_the_run(tmp_path: Path) -> None:
    log = make(tmp_path)
    # A directory at the projection path makes the append fail.
    (tmp_path / "routes.jsonl").mkdir()
    entry = log.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=decision(),
    )
    assert entry.seq == 1  # the decision still succeeded
    warnings = [
        e for e in read_events(tmp_path / "events.jsonl") if e.type == "route.log.write_failed"
    ]
    assert len(warnings) == 1
    assert warnings[0].data["node_id"] == "n1"
    assert warnings[0].data["route_seq"] == 1
    # The canonical event is still there, so the projection can be rebuilt later.
    assert len(routed_events(tmp_path / "events.jsonl")) == 1


def test_missing_projections_rebuild_idempotently_by_seq(tmp_path: Path) -> None:
    log = make(tmp_path)
    for i in range(3):
        log.route(
            node_id="n1",
            attempt=i + 1,
            event="routed",
            category="implement",
            complexity="low",
            agent_decision=decision(),
            model_decision=model(),
        )
    routes_path = tmp_path / "routes.jsonl"
    route_log_path = tmp_path / "route.log"
    original_routes = routes_path.read_bytes()
    original_log = route_log_path.read_bytes()

    routes_path.unlink()
    route_log_path.unlink()
    assert not routes_path.exists() and not route_log_path.exists()

    assert rebuild_projections(tmp_path / "events.jsonl", routes_path, route_log_path) == 3
    assert routes_path.read_bytes() == original_routes
    assert route_log_path.read_bytes() == original_log

    # Rebuilding again is a no-op (idempotent).
    assert rebuild_projections(tmp_path / "events.jsonl", routes_path, route_log_path) == 3
    assert routes_path.read_bytes() == original_routes


def test_rebuild_discards_only_a_torn_trailing_record(tmp_path: Path) -> None:
    log = make(tmp_path)
    log.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=decision(),
    )
    events_path = tmp_path / "events.jsonl"
    good = events_path.read_bytes()
    # A torn trailing fragment of a second event is discarded, never an earlier valid entry.
    events_path.write_bytes(good + b'{"seq":2,"ts":"2026-10-22T09:4')
    assert rebuild_projections(events_path, tmp_path / "routes.jsonl", tmp_path / "route.log") == 1
    assert len(routes_lines(tmp_path / "routes.jsonl")) == 1


def test_enabled_false_disables_projections_but_not_the_canonical_event(tmp_path: Path) -> None:
    log = make(tmp_path, enabled=False)
    log.route(
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=decision(),
        model_decision=model(),
    )
    assert len(routed_events(tmp_path / "events.jsonl")) == 1
    assert not (tmp_path / "routes.jsonl").exists()
    assert not (tmp_path / "route.log").exists()
    # Rebuild also honours the flag: no projections are written.
    assert (
        rebuild_projections(
            tmp_path / "events.jsonl",
            tmp_path / "routes.jsonl",
            tmp_path / "route.log",
            enabled=False,
        )
        == 1
    )
    assert not (tmp_path / "routes.jsonl").exists()


# --- route.log matches routes.jsonl ---------------------------------------------------------


def test_route_log_lines_match_routes_jsonl(tmp_path: Path) -> None:
    log = make(tmp_path)
    log.route(
        node_id="n2-impl-a",
        attempt=1,
        event="routed",
        category="implement",
        complexity="high",
        agent_decision=decision(),
        model_decision=model(),
        title="Debounce autosave handler",
    )
    log.route(
        node_id="n2-impl-a",
        attempt=2,
        event="fallback",
        category="implement",
        complexity="high",
        agent_decision=decision(agent="claude", agent_rule="fallback", reason="timeout"),
        model_decision=model(model="claude-deep", model_rule="claude-impl", effort=None),
        title="Debounce autosave handler",
        previous=PreviousRoute(agent="codex", model="codex-deep", reason="timeout"),
    )
    entries = routes_lines(tmp_path / "routes.jsonl")
    lines = log_lines(tmp_path / "route.log")
    assert len(entries) == len(lines) == 2

    for entry, line in zip(entries, lines, strict=True):
        assert line.startswith(entry["ts"])
        assert f"#{entry['seq']}" in line
        assert entry["node_id"] in line
        assert f"{entry['category']}/{entry['complexity']}" in line
        assert entry["agent"] in line
        assert entry["model"] in line
        assert f"agent_rule={entry['agent_rule']}" in line
        assert f"model_rule={entry['model_rule']}" in line
        if entry["title"]:
            assert f'"{entry["title"]}"' in line

    # The fallback line carries its `from` source.
    assert "(from codex: timeout)" in lines[1]


# --- catalog completeness --------------------------------------------------------------------


def test_catalog_covers_the_events_the_route_log_touches() -> None:
    assert "node.routed" in CATALOG
    assert "route.log.write_failed" in CATALOG
    assert CATALOG["node.routed"].required == frozenset({"node_id", "route_seq", "decision"})


def test_reason_and_alternatives_round_trip_through_the_entry_schema() -> None:
    entry = build_entry(
        run_id=RUN_ID,
        node_id="n1",
        attempt=1,
        event="routed",
        category="implement",
        complexity="low",
        agent_decision=decision(alternatives=(Alternative(agent="claude", score=-0.2),)),
        model_decision=model(),
        title="Some task",
    )
    round_tripped = RouteEntry.model_validate(entry.model_dump(mode="json", by_alias=True))
    assert round_tripped.alternatives[0].score == -0.2
    assert round_tripped.from_ is None
