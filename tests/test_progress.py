"""Worker progress: sanitization, bounded delivery, streaming and backpressure (task 2.26)."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from harness import init_repo
from relay.adapters.base import ProcessResult
from relay.adapters.claude import ClaudeAdapter
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.core.controller import SessionController
from relay.core.platform.proc import (
    OutputLimitExceeded,
    ProcessCancelled,
    cancel_processes_when,
    run_process,
    stream_output,
)
from relay.core.progress import (
    ClaudeStreamParser,
    CodexEventParser,
    ProgressChannel,
    ProgressRecord,
    sanitize_text,
    split_frames,
)
from relay.core.registry import Agent, Config, validate_merged
from relay.core.sessions import SessionStore
from relay.core.specs import Graph

DIR = Path(__file__).parent / "fixtures" / "recorded"
IDS: dict[str, Any] = {"run_id": "r1", "node_id": "n1", "attempt": 1}


# --- sanitization and framing ------------------------------------------------------------------


def test_terminal_escapes_and_controls_are_stripped() -> None:
    raw = "\x1b[31mred\x1b[0m \x1b]0;evil title\x07ok\x1b]8;;http://x\x1b\\link \x00\x07\x08 done\r\nnext"
    clean = sanitize_text(raw)
    assert clean == "red oklink  done\nnext"
    assert "\x1b" not in clean and "\x07" not in clean


def test_bidi_overrides_and_markup_are_inert() -> None:
    assert sanitize_text("a‮b⁦c") == "abc"
    assert sanitize_text("<script>[red]x[/red]</script>") == "<script>[red]x[/red]</script>"


def test_secrets_are_redacted() -> None:
    text = sanitize_text(
        "token sk-ant-api03-abcdefghijklmnop and password=hunter2 and ghp_" + "a" * 36
    )
    assert "hunter2" not in text and "abcdefghijklmnop" not in text and "ghp_a" not in text


def test_frames_respect_the_byte_limit_and_utf8_boundaries() -> None:
    text = "é😀日本語" * 700
    frames = split_frames(text, 100)
    assert "".join(frames) == text
    assert all(len(f.encode("utf-8")) <= 100 for f in frames)
    assert split_frames("", 100) == [""]
    with pytest.raises(ValueError):
        split_frames("x", 2)


# --- the channel -------------------------------------------------------------------------------


def test_oversized_text_is_split_into_bounded_frames() -> None:
    channel = ProgressChannel(frame_bytes=1000)
    sub = channel.subscribe()
    channel.publish("text", "a" * 2500, **IDS)
    records = sub.drain()
    assert [len(r.text) for r in records] == [1000, 1000, 500]
    assert [r.seq for r in records] == [1, 2, 3]
    assert all(r.run_id == "r1" and r.node_id == "n1" and r.attempt == 1 for r in records)


def test_a_full_queue_drops_transient_frames_behind_an_explicit_gap() -> None:
    channel = ProgressChannel(queue_records=8)
    sub = channel.subscribe()
    channel.publish("phase", "agent", **IDS)
    for index in range(500):
        # a distinct source per record so nothing coalesces
        channel.publish("tool", f"step {index}", run_id="r1", node_id=f"n{index}", attempt=1)
    records = sub.drain()
    assert len(records) <= 9  # capacity plus the gap marker
    assert records[0].kind == "gap" and records[0].dropped > 400
    assert any(r.kind == "phase" for r in records)  # lifecycle is never the first thing dropped
    assert sub.drain() == []  # the gap is reported once


def test_consecutive_text_is_coalesced_under_pressure() -> None:
    channel = ProgressChannel(queue_records=8, frame_bytes=1000)
    sub = channel.subscribe()
    for _ in range(40):
        channel.publish("text", "line\n", **IDS)
    records = sub.drain()
    assert records[0].kind == "gap"
    assert sum(r.text.count("line") for r in records if r.kind == "text") <= 40
    assert len(records) <= 9


def test_a_subscriber_that_never_reads_cannot_slow_producers() -> None:
    channel = ProgressChannel()
    stuck = channel.subscribe()  # never drained
    started = time.monotonic()
    for index in range(20_000):
        channel.publish("text", f"frame {index}", **IDS)
    assert time.monotonic() - started < 10
    assert len(stuck.drain()) <= 257


def test_closed_and_unsubscribed_consumers_are_ignored() -> None:
    channel = ProgressChannel()
    sub = channel.subscribe()
    channel.unsubscribe(sub)
    channel.publish("text", "x", **IDS)
    assert sub.closed and sub.drain() == []


def test_async_consumer_wakes_on_cross_thread_publish() -> None:
    async def scenario() -> None:
        channel = ProgressChannel()
        sub = channel.subscribe()
        sub.bind(asyncio.get_running_loop())
        threading.Timer(0.1, lambda: channel.publish("text", "hello", **IDS)).start()
        batch = await sub.next_batch(timeout_s=5)
        assert [r.text for r in batch] == ["hello"]
        assert await sub.next_batch(timeout_s=0.1) == []

    asyncio.run(scenario())


def test_publish_is_safe_from_many_threads() -> None:
    channel = ProgressChannel(queue_records=64)
    sub = channel.subscribe()
    errors: list[BaseException] = []

    def worker(name: int) -> None:
        try:
            for index in range(500):
                channel.publish("text", f"{name}:{index}", run_id="r", node_id=str(name), attempt=1)
                if index % 50 == 0:
                    sub.drain()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


# --- parsers -----------------------------------------------------------------------------------


def test_claude_stream_events_are_normalized() -> None:
    parser = ClaudeStreamParser()
    assert parser.feed(json.dumps({"type": "system", "subtype": "init", "model": "m1"})) == [
        ("phase", "agent started (m1)")
    ]
    assert parser.feed(
        json.dumps({"type": "system", "subtype": "hook_progress", "stdout": "x" * 9999})
    ) == [("diagnostic", "system event hook_progress")]
    assistant = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "private"},
                {"type": "text", "text": "Looking at the handler"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "pytest " + "x" * 400}},
            ]
        },
    }
    updates = parser.feed(json.dumps(assistant))
    assert updates[0] == ("text", "Looking at the handler")
    assert updates[1][0] == "tool" and updates[1][1].startswith("Bash: pytest ")
    assert len(updates[1][1]) <= 200 and "private" not in str(updates)
    user = {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True}]}}
    assert parser.feed(json.dumps(user)) == [("tool", "tool result (error)")]
    assert parser.feed(json.dumps({"type": "stream_event", "event": {}})) == []
    assert parser.feed(json.dumps({"type": "result", "result": "ok"})) == [
        ("phase", "agent finished")
    ]


def test_unknown_and_malformed_lines_are_only_diagnostics() -> None:
    for parser in (ClaudeStreamParser(), CodexEventParser()):
        assert parser.feed("not json at all") == []
        assert parser.feed("") == []
        updates = parser.feed(json.dumps({"type": "launch_missiles", "now": True}))
        assert updates == [("diagnostic", "unknown event launch_missiles")]
        # whatever a stream says, the only possible kinds are display kinds
        assert {kind for kind, _ in updates} <= {"text", "tool", "phase", "diagnostic"}


def test_codex_recorded_events_are_normalized() -> None:
    parser = CodexEventParser()
    lines = (DIR / "codex_schema_needs_input.jsonl").read_text(encoding="utf-8").splitlines()
    updates = [u for line in lines for u in parser.feed(line)]
    kinds = [kind for kind, _ in updates]
    assert kinds[0] == "phase" and "text" in kinds and "tool" in kinds and kinds[-1] == "phase"
    assert any(text.startswith("$ ") for kind, text in updates if kind == "tool")


# --- streaming through the process supervisor --------------------------------------------------


def child(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def test_lines_reach_the_sink_while_the_process_still_runs(tmp_path: Path) -> None:
    code = "import time\nprint('first', flush=True)\ntime.sleep(1.5)\nprint('second', flush=True)"
    seen: list[tuple[str, float]] = []
    started = time.monotonic()
    with stream_output(lambda line: seen.append((line.strip(), time.monotonic() - started))):
        result = run_process(child(code), cwd=tmp_path, env=dict(__import__("os").environ))
    assert [text for text, _ in seen] == ["first", "second"]
    assert seen[0][1] < seen[1][1] - 1.0  # delivered live, not at exit
    assert result.stdout.split() == ["first", "second"] and result.exit_code == 0


def test_a_failing_or_slow_sink_never_disturbs_capture(tmp_path: Path) -> None:
    code = "for i in range(3000): print(i)"
    calls = 0

    def broken(_line: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("renderer crashed")

    with stream_output(broken):
        result = run_process(child(code), cwd=tmp_path, env=dict(__import__("os").environ))
    assert calls == 3000 and result.stdout.split() == [str(i) for i in range(3000)]


def test_flooding_output_is_fully_captured_and_the_channel_stays_bounded(tmp_path: Path) -> None:
    code = "import sys\nsys.stdout.write(('y' * 99 + '\\n') * 150000)"
    channel = ProgressChannel()
    sub = channel.subscribe()  # never read: the worst-case slow UI
    tap_calls = 0

    def sink(line: str) -> None:
        nonlocal tap_calls
        tap_calls += 1
        channel.publish("text", line, **IDS)

    with stream_output(sink):
        result = run_process(
            child(code), cwd=tmp_path, env=dict(__import__("os").environ), timeout_s=120
        )
    assert result.exit_code == 0 and len(result.stdout) == 150000 * 100
    assert tap_calls == 150000
    assert len(sub.drain()) <= 257


def test_capture_limit_fails_explicitly_instead_of_truncating(tmp_path: Path) -> None:
    code = "import time\nprint('z' * 1000, flush=True)\nwhile True:\n    print('z' * 1000, flush=True)\n    time.sleep(0.001)"
    started = time.monotonic()
    with stream_output(lambda _l: None, max_bytes=50_000), pytest.raises(OutputLimitExceeded):
        run_process(child(code), cwd=tmp_path, env=dict(__import__("os").environ), timeout_s=60)
    assert time.monotonic() - started < 30  # the runaway tree was stopped


def test_streaming_still_honours_cancellation_and_timeouts(tmp_path: Path) -> None:
    hang = "import time\nprint('up', flush=True)\ntime.sleep(60)"
    env = dict(__import__("os").environ)
    with stream_output(lambda _l: None):
        timed = run_process(child(hang), cwd=tmp_path, env=env, timeout_s=1.0)
    assert timed.timed_out and "up" in timed.stdout

    event = threading.Event()
    threading.Timer(0.8, event.set).start()
    with (
        stream_output(lambda _l: None),
        cancel_processes_when(event),
        pytest.raises(ProcessCancelled),
    ):
        run_process(child(hang), cwd=tmp_path, env=env)


# --- adapters: streaming and structured output qualify together --------------------------------


def test_claude_streaming_invocation_still_parses_the_final_result(tmp_path: Path) -> None:
    from test_claude_adapter import claude_fixture, ctx, spec

    adapter = ClaudeAdapter(Agent(cmd="claude"), parent_env={}, stream_progress=True)
    invocation = adapter.build_invocation(spec(), ctx(tmp_path))
    args = invocation.args
    assert args[args.index("--output-format") + 1] == "stream-json" and "--verbose" in args
    assert "--json-schema" in args
    assert isinstance(adapter.progress_parser(), ClaudeStreamParser)
    assert ClaudeAdapter(Agent(cmd="claude"), parent_env={}).progress_parser() is None

    final = claude_fixture("claude_schema_done.json")
    final["type"] = final.get("type", "result")
    lines = [
        json.dumps({"type": "system", "subtype": "init", "model": "m"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps(final),
    ]
    proc = ProcessResult(exit_code=0, stdout="\n".join(lines) + "\n", duration_s=1.0)
    result = asyncio.run(adapter.parse_result(proc, spec(), ctx(tmp_path)))
    assert result.status == "done" and result.session is not None


# --- end to end through a graph round -----------------------------------------------------------


def config() -> Config:
    return validate_merged(
        {
            "version": 1,
            "defaults": {"concurrency": 2, "fallback": ["claude", "codex"]},
            "agents": {
                key: {"cmd": "fake", "concurrency": 2, "models": {"balanced": "default"}}
                for key in ("claude", "codex")
            },
        }
    )


def one_node() -> Graph:
    return Graph.model_validate(
        {
            "verify": "none",
            "verification_skip_reason": "progress test",
            "nodes": [{"id": "a", "category": "implement", "goal": "x", "owns": ["a/**"]}],
        }
    )


async def round_with(adapter: FakeAdapter, repo: Path) -> tuple[list[ProgressRecord], Any]:
    owner = SessionStore.create(repo).acquire_owner()
    spec = one_node()
    revision = owner.save_plan(
        spec.model_dump(mode="json", by_alias=True, exclude_unset=True), explanation="t"
    )
    owner.approve(
        revision,
        owner.store.plan(revision)["digest"],
        idempotency_key="k",
        expected_session_revision=owner.record.revision,
    )
    ctl = SessionController(owner, config=config(), adapters={"claude": adapter, "codex": adapter})
    sub = ctl.progress.subscribe()
    await ctl.start_round()
    collected: list[ProgressRecord] = []
    finishing = asyncio.create_task(ctl.finish_round())
    while not finishing.done():
        collected.extend(await sub.next_batch(timeout_s=0.2))
    collected.extend(sub.drain())
    result = await finishing
    await ctl.shutdown()
    return collected, result


def run(coro: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(coro())


def test_round_publishes_phases_and_streamed_text_with_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        adapter = FakeAdapter(
            {
                "a": Scenario(
                    write={"a/x.py": "x"}, stream_lines=["step one", "step two"], stream_delay_s=0.2
                )
            },
            progress=True,
        )
        records, result = await round_with(adapter, repo)
        assert result.exit_code == 0, result.to_json()
        texts = [r.text.strip() for r in records if r.kind == "text"]
        assert "step one" in texts and "step two" in texts
        phases = [r.text for r in records if r.kind == "phase"]
        assert phases == ["agent", "capture", "publish"] or phases[0] == "agent"
        assert {"agent", "capture", "publish"} <= set(phases)
        assert all(r.node_id == "a" and r.attempt == 1 and r.run_id for r in records)

    run(scenario)


def test_unsupported_streaming_shows_lifecycle_only(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        adapter = FakeAdapter(
            {"a": Scenario(write={"a/x.py": "x"}, stream_lines=["hidden chatter"])}, progress=False
        )
        records, result = await round_with(adapter, repo)
        assert result.exit_code == 0
        assert {r.kind for r in records} == {"phase"}
        assert not any("hidden chatter" in r.text for r in records)

    run(scenario)


def test_a_flooding_worker_with_a_stuck_subscriber_still_completes(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        spec = one_node()
        revision = owner.save_plan(
            spec.model_dump(mode="json", by_alias=True, exclude_unset=True), explanation="t"
        )
        owner.approve(
            revision,
            owner.store.plan(revision)["digest"],
            idempotency_key="k",
            expected_session_revision=owner.record.revision,
        )
        adapter = FakeAdapter(
            {"a": Scenario(write={"a/x.py": "x"}, flood_lines=60000, flood_bytes=100)},
            progress=True,
        )
        ctl = SessionController(
            owner, config=config(), adapters={"claude": adapter, "codex": adapter}
        )
        stuck = ctl.progress.subscribe()  # never read
        await ctl.start_round()
        result = await asyncio.wait_for(ctl.finish_round(), timeout=120)
        assert result.exit_code == 0, result.to_json()
        assert len(stuck.drain()) <= 257
        await ctl.shutdown()

    run(scenario)


@pytest.mark.skipif(
    __import__("os").environ.get("RELAY_LIVE") != "1"
    or __import__("shutil").which("claude") is None,
    reason="live Claude streaming check is opt-in (RELAY_LIVE=1)",
)
def test_live_claude_streaming_yields_progress_and_the_final_result(tmp_path: Path) -> None:
    """Qualifies streaming and structured output together on the installed CLI (design 7.4)."""
    import subprocess

    from relay.adapters.base import RunContext, run_invocation
    from relay.core.progress import ProgressTap
    from relay.core.specs import TaskSpec

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    node = tmp_path / "node"
    node.mkdir()
    adapter = ClaudeAdapter(Agent(cmd="claude"), stream_progress=True)
    task = TaskSpec(
        id="n1",
        run_id="r",
        category="docs-for-code",
        goal="Reply done with summary 'live stream ok'. Do not edit files.",
    )
    ctx = RunContext("r", "n1", 1, node, tmp_path, permission="readonly")
    channel = ProgressChannel()
    sub = channel.subscribe()
    tap = ProgressTap(channel, adapter.progress_parser(), run_id="r", node_id="n1", attempt=1)
    with stream_output(tap.feed):
        proc = run_invocation(adapter.build_invocation(task, ctx), timeout_s=240)
    kinds = {r.kind for r in sub.drain()}
    assert "phase" in kinds
    result = asyncio.run(adapter.parse_result(proc, task, ctx))
    assert result.status == "done"
