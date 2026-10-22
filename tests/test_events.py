from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from design_blocks import blocks_in, design_blocks
from relay.core.events import (
    CATALOG,
    EventError,
    EventLog,
    EventLogCorrupt,
    read_events,
    replay,
    scan,
)

CLOCK = datetime(2026, 10, 22, 9, 41, 3, 123000, tzinfo=UTC)


def log_at(path: Path, **kwargs: Any) -> EventLog:
    return EventLog(path, now=lambda: CLOCK, **kwargs)


def route_decision() -> dict[str, Any]:
    (block,) = blocks_in("4.4", "json")
    return json.loads(block.text)


def state(node: str = "n1", to: str = "running") -> dict[str, Any]:
    return {"node_id": node, "from": "pending", "to": to}


# --- appending --------------------------------------------------------------------------------


def test_events_get_monotonic_seq_from_one(tmp_path: Path) -> None:
    with log_at(tmp_path / "events.jsonl") as log:
        events = [log.append("node.state", state(to=s)) for s in ("running", "done")]
    assert [e.seq for e in events] == [1, 2]
    assert [e.seq for e in read_events(tmp_path / "events.jsonl")] == [1, 2]


def test_record_is_one_flat_line_with_envelope(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with log_at(path, owner_generation=3) as log:
        log.append("node.state", {**state(), "reason": "start"}, hashes={"task": "ab12"})
    (line,) = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(line) == {
        "seq": 1,
        "ts": "2026-10-22T09:41:03.123Z",
        "type": "node.state",
        "owner_generation": 3,
        "hashes": {"task": "ab12"},
        "node_id": "n1",
        "from": "pending",
        "to": "running",
        "reason": "start",
    }


def test_reopening_continues_the_sequence(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with log_at(path) as log:
        log.append("warning", {"message": "a"})
    with log_at(path) as log:
        assert log.last_seq == 1
        assert log.append("warning", {"message": "b"}).seq == 2
    assert [e.data["message"] for e in read_events(path)] == ["a", "b"]


def test_append_is_on_disk_before_returning(tmp_path: Path) -> None:
    """The routing entry must survive an immediate kill (section 4.4), so no buffering."""
    path = tmp_path / "events.jsonl"
    log = log_at(path)
    log.append("warning", {"message": "x"})
    assert len(path.read_bytes().splitlines()) == 1  # visible without close()
    log.close()


KILLER = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from relay.core.events import EventLog

    log = EventLog(Path(sys.argv[1]))
    for i in range(5):
        log.append("warning", {"message": f"m{i}"})
    print("done", flush=True)
    os._exit(0)  # no close, no flush: a crash right after the append
    """
)


def test_events_survive_a_hard_exit_of_the_writer(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    subprocess.run([sys.executable, "-I", "-c", KILLER, str(path)], check=True)
    assert [e.data["message"] for e in read_events(path)] == [f"m{i}" for i in range(5)]


# --- catalog ----------------------------------------------------------------------------------


def test_unknown_type_is_rejected(tmp_path: Path) -> None:
    with log_at(tmp_path / "e.jsonl") as log, pytest.raises(EventError, match="unknown event type"):
        log.append("node.exploded", {})


@pytest.mark.parametrize(
    ("type_", "data", "message"),
    [
        ("node.state", {"node_id": "n1", "from": "pending"}, "missing fields"),
        ("node.state", {**state(), "extra": 1}, "unexpected fields"),
        ("node.state", {**state(), "seq": 9}, "envelope"),
        ("node.state", {**state(), "type": "x"}, "envelope"),
        (
            "input.recorded",
            {
                "node_id": "n",
                "question_revision": 1,
                "submission_id": "s",
                "provenance": "operator-tty",
                "answers": {"q1": "secret"},
            },
            "answers",
        ),
        ("apply.started", {"op_id": "o", "target": "main"}, "missing fields"),
    ],
)
def test_payloads_are_checked_against_the_catalog(
    tmp_path: Path, type_: str, data: dict[str, Any], message: str
) -> None:
    with log_at(tmp_path / "e.jsonl") as log, pytest.raises(EventError, match=message):
        log.append(type_, data)
    assert read_events(tmp_path / "e.jsonl") == []  # nothing written for a rejected event


def test_open_event_types_accept_extra_fields(tmp_path: Path) -> None:
    with log_at(tmp_path / "e.jsonl") as log:
        log.append("warning", {"message": "m", "code": 7})
        log.append("verify.result", {"node_id": "n1", "passed": True})


def test_input_recorded_carries_only_the_hash(tmp_path: Path) -> None:
    with log_at(tmp_path / "e.jsonl") as log:
        event = log.append(
            "input.recorded",
            {
                "node_id": "n1",
                "question_revision": 1,
                "submission_id": "s1",
                "provenance": "operator-tty",
                "content_hash": "ab" * 32,
            },
        )
    assert "answers" not in event.to_json()


def test_node_routed_decision_is_validated_as_a_route_entry(tmp_path: Path) -> None:
    with log_at(tmp_path / "e.jsonl") as log:
        event = log.append(
            "node.routed", {"node_id": "n2-impl-a", "route_seq": 7, "decision": route_decision()}
        )
        assert event.data["decision"]["agent_rule"] == "route-impl-codex"
        bad = {**route_decision(), "event": "fallback"}  # fallback needs "from"
        with pytest.raises(EventError, match="invalid decision"):
            log.append("node.routed", {"node_id": "n2", "route_seq": 8, "decision": bad})


def test_catalog_covers_every_event_the_design_names() -> None:
    """Section 11 catalog completeness: each backticked event type in the design is present."""
    import re

    design = (Path(__file__).resolve().parents[1] / "docs" / "design.md").read_text("utf-8")
    section = design[design.index("## 11. Observability") : design.index("## 12.")]
    named = set(re.findall(r"`([a-z]+(?:\.[a-z_]+)+)(?: \{|`|,)", section))
    named |= {"budget.reserved", "budget.settled", "budget.exhausted", "budget.overrun"}
    named |= {"spawn.accepted", "spawn.rejected", "node.reused", "node.invalidated"}
    named -= {"events.jsonl", "routes.jsonl", "run.json"}
    missing = {
        n
        for n in named
        if n not in CATALOG and n.split(".")[0] in {c.split(".")[0] for c in CATALOG}
    }
    assert not missing, f"design names events missing from the catalog: {sorted(missing)}"
    assert design_blocks()  # the design still parses


# --- torn tail and corruption -----------------------------------------------------------------


def write_two(path: Path) -> bytes:
    with log_at(path) as log:
        log.append("warning", {"message": "one"})
        log.append("warning", {"message": "two"})
    return path.read_bytes()


def test_torn_trailing_record_is_discarded_on_open(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    good = write_two(path)
    path.write_bytes(good + b'{"seq":3,"ts":"2026-10-22T09:4')  # crash mid-append
    assert [e.seq for e in read_events(path)] == [1, 2]  # readers ignore it without editing
    assert path.read_bytes().endswith(b'"2026-10-22T09:4')
    with log_at(path) as log:
        assert path.read_bytes() == good  # writer truncated only the fragment
        assert log.append("warning", {"message": "three"}).seq == 3
    assert [e.seq for e in read_events(path)] == [1, 2, 3]


def test_complete_record_missing_only_its_newline_is_repaired(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    good = write_two(path)
    path.write_bytes(good.rstrip(b"\n"))
    with log_at(path) as log:
        assert log.last_seq == 2  # the second event was complete, so it is kept
        log.append("warning", {"message": "three"})
    assert [e.data["message"] for e in read_events(path)] == ["one", "two", "three"]
    assert path.read_bytes().count(b"\n") == 3


@pytest.mark.parametrize("fragment", [b"{", b'{"seq":3', b"garbage", b"   "])
def test_any_trailing_fragment_without_newline_is_torn(tmp_path: Path, fragment: bytes) -> None:
    path = tmp_path / "events.jsonl"
    good = write_two(path)
    path.write_bytes(good + fragment)
    result = scan(path)
    assert [e.seq for e in result.events] == [1, 2]
    assert result.good_length == len(good)


def test_corrupt_earlier_record_is_an_error_not_skipped(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    lines = write_two(path).splitlines()
    path.write_bytes(b"\n".join([b"{not json", lines[1]]) + b"\n")
    with pytest.raises(EventLogCorrupt, match="unparsable"):
        read_events(path)
    with pytest.raises(EventLogCorrupt):
        log_at(path)


def test_gaps_and_repeats_in_seq_are_detected(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    lines = write_two(path).splitlines()
    path.write_bytes(lines[1] + b"\n")  # seq 2 first: a gap
    with pytest.raises(EventLogCorrupt, match="expected seq 1"):
        read_events(path)
    path.write_bytes(lines[0] + b"\n" + lines[0] + b"\n")  # duplicate
    with pytest.raises(EventLogCorrupt, match="expected seq 2"):
        read_events(path)


def test_missing_log_reads_as_empty(tmp_path: Path) -> None:
    assert read_events(tmp_path / "nope.jsonl") == []


# --- idempotent replay ------------------------------------------------------------------------


def test_replay_applies_each_event_once_across_runs(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    projection: dict[int, str] = {}

    def apply(event: Any) -> None:
        assert event.seq not in projection, "event applied twice"
        projection[event.seq] = event.data["message"]

    with log_at(path) as log:
        log.append("warning", {"message": "a"})
        log.append("warning", {"message": "b"})
        applied = replay(path, apply)
        assert applied == 2
        assert replay(path, apply, after_seq=applied) == 2  # nothing new, no double apply
        log.append("warning", {"message": "c"})
        assert replay(path, apply, after_seq=applied) == 3
    assert projection == {1: "a", 2: "b", 3: "c"}


def test_replay_ignores_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(write_two(path) + b'{"seq":3')
    seen: list[int] = []
    assert replay(path, lambda e: seen.append(e.seq)) == 2
    assert seen == [1, 2]
