from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harness import context, git, init_repo, run_invocation, task
from relay.adapters.base import (
    RESULT_FILE,
    Invocation,
    ProcessResult,
    RunContext,
    archive_previous_result,
    start_attempt,
)
from relay.adapters.fake import FakeAdapter, ResultInvalid
from relay.adapters.fake_scenario import Scenario
from relay.core.specs import TaskSpec


def run(adapter: FakeAdapter, tmp: Path, *, attempt: int = 1, node: str = "n1", **kw: object):  # noqa: ANN201
    """Build, execute and parse one attempt; returns (result, process result, context)."""
    ctx = context(tmp, node, attempt, resume_session=kw.get("resume_session"))  # type: ignore[arg-type]
    spec = task(node)
    proc = run_invocation(start_attempt(adapter, spec, ctx))
    result = asyncio.run(adapter.parse_result(proc, spec, ctx))
    return result, proc, ctx


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    init_repo(tmp_path / "worktree")
    return tmp_path


# --- invocation -------------------------------------------------------------------------------


def test_invocation_runs_the_stub_in_the_worktree(repo: Path) -> None:
    adapter = FakeAdapter()
    ctx = context(repo)
    inv = adapter.build_invocation(task(goal="Add debounce"), ctx)
    assert inv.cwd == ctx.worktree
    assert inv.stdin == "Add debounce\n"
    assert "relay.adapters.fake_cli" in inv.args
    assert json.loads((ctx.node_dir / "fake-scenario.json").read_text())["runtime"]["attempt"] == 1
    assert adapter.invocations == [inv]


# --- what the stub agent does -----------------------------------------------------------------


def test_stub_writes_modifies_and_deletes_files(repo: Path) -> None:
    scenario = Scenario(write={"src/a.txt": "A\n", "README.md": "changed\n"}, delete=["README.md"])
    run(FakeAdapter(scenario), repo)
    work = repo / "worktree"
    assert (work / "src" / "a.txt").read_text() == "A\n"
    assert not (work / "README.md").exists()
    assert "src/a.txt" in git(
        work, "status", "--porcelain", "-uall"
    )  # uncommitted, like a real agent


def test_stub_can_commit_like_a_self_committing_agent(repo: Path) -> None:
    run(FakeAdapter(Scenario(write={"x.txt": "x\n"}, commit="agent commit")), repo)
    work = repo / "worktree"
    assert git(work, "log", "-1", "--format=%s") == "agent commit"
    assert git(work, "status", "--porcelain") == ""


def test_stub_records_prompt_resume_session_and_selected_env(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELAY_TEST_VISIBLE", "yes")
    scenario = Scenario(echo_env=["RELAY_TEST_VISIBLE", "RELAY_TEST_ABSENT"])
    _, _, ctx = run(FakeAdapter(scenario), repo, resume_session="sess-9")
    observed = json.loads((ctx.node_dir / "fake-observed.json").read_text())
    assert observed["env"] == {"RELAY_TEST_VISIBLE": "yes", "RELAY_TEST_ABSENT": None}
    assert observed["resume_session"] == "sess-9"
    assert (ctx.node_dir / "fake-stdin.txt").read_text() == "Do the thing\n"


# --- output modes and parsing order (design 7.3) ----------------------------------------------


@pytest.mark.parametrize("mode", ["json", "file", "block"])
def test_every_output_mode_yields_the_same_result(repo: Path, mode: str) -> None:
    result, _, _ = run(FakeAdapter(Scenario(output=mode, summary="did it")), repo)  # type: ignore[arg-type]
    assert (result.status, result.summary, result.agent) == ("done", "did it", "fake")
    assert result.session is not None
    assert result.session.cli_session_id == "fake-n1-1"


def test_no_result_is_synthesized_from_exit_code_and_last_line(repo: Path) -> None:
    ok, _, _ = run(FakeAdapter(Scenario(output="none", stdout="a\nlast line")), repo)
    assert (ok.status, ok.summary) == ("done", "last line")
    bad, proc, _ = run(FakeAdapter(Scenario(output="none", exit_code=3)), repo)
    assert proc.exit_code == 3
    assert bad.status == "failed"


def test_failure_with_a_native_result_keeps_the_agents_status(repo: Path) -> None:
    result, proc, _ = run(FakeAdapter(Scenario(status="failed", exit_code=1)), repo)
    assert (proc.exit_code, result.status) == (1, "failed")


@pytest.mark.parametrize("native", [True, False])
def test_valid_result_ignores_malformed_lower_priority_sources(
    tmp_path: Path, native: bool
) -> None:
    ctx = context(tmp_path)
    candidate = {"id": "n1", "status": "done", "summary": "current"}
    (ctx.node_dir / RESULT_FILE).write_text(
        "status: invalid" if native else json.dumps(candidate), encoding="utf-8"
    )
    stdout = "```yaml\nstatus: invalid\n```"
    if native:
        stdout += "\n" + json.dumps({"result": candidate})
    proc = ProcessResult(exit_code=0, stdout=stdout, stderr="", duration_s=0)
    result = asyncio.run(FakeAdapter().parse_result(proc, task(), ctx))
    assert (result.status, result.summary) == ("done", "current")


@pytest.mark.parametrize(
    "content",
    [b"status: invalid", b"id: [unclosed", b"", "id: n1".encode("utf-16")],
    ids=["schema", "yaml", "empty", "utf-16"],
)
def test_malformed_node_file_raises_result_invalid(tmp_path: Path, content: bytes) -> None:
    ctx = context(tmp_path)
    (ctx.node_dir / RESULT_FILE).write_bytes(content)
    proc = ProcessResult(exit_code=0, stdout="", stderr="", duration_s=0)
    with pytest.raises(ResultInvalid):
        asyncio.run(FakeAdapter().parse_result(proc, task(), ctx))


@pytest.mark.parametrize("timeout", [False, True])
def test_retry_does_not_parse_previous_attempt_result(repo: Path, timeout: bool) -> None:
    adapter = FakeAdapter(
        [
            Scenario(output="file", summary="previous attempt"),
            Scenario(output="none", exit_code=3, hang=timeout),
        ]
    )
    first, _, ctx = run(adapter, repo)
    assert first.status == "done"
    assert (ctx.node_dir / RESULT_FILE).is_file()
    ctx = context(repo, attempt=2)
    invocation = start_attempt(adapter, task(), ctx)
    archived = ctx.node_dir / "attempts" / "01" / RESULT_FILE
    assert "previous attempt" in archived.read_text(encoding="utf-8")
    assert not (ctx.node_dir / RESULT_FILE).exists()
    proc = run_invocation(invocation, timeout_s=1.5 if timeout else 30)
    assert proc.timed_out == timeout
    result = asyncio.run(adapter.parse_result(proc, task(), ctx))
    assert result.status == "failed"
    assert result.summary != "previous attempt"


def test_first_attempt_archives_nothing(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    (ctx.node_dir / RESULT_FILE).write_text("{}", encoding="utf-8")
    assert archive_previous_result(ctx) is None
    assert (ctx.node_dir / RESULT_FILE).is_file()


def test_archive_never_overwrites_an_earlier_result(tmp_path: Path) -> None:
    ctx = context(tmp_path, attempt=2)
    archived: list[Path | None] = []
    for text in ("questions", "re-entered attempt"):
        (ctx.node_dir / RESULT_FILE).write_text(text, encoding="utf-8")
        archived.append(archive_previous_result(ctx))
    directory = ctx.node_dir / "attempts" / "01"
    assert archived == [directory / RESULT_FILE, directory / "result-2.spec.yaml"]
    assert [path.read_text(encoding="utf-8") for path in archived if path] == [
        "questions",
        "re-entered attempt",
    ]


def test_needs_input_returns_typed_questions(repo: Path) -> None:
    questions = [
        {
            "id": "q1",
            "text": "Which cache?",
            "kind": "choice",
            "options": ["redis", "lru"],
            "default": "lru",
            "required": True,
        },
        {"id": "q2", "text": "Any constraints?", "kind": "free", "required": False},
    ]
    result, _, _ = run(FakeAdapter(Scenario(status="needs_input", questions=questions)), repo)
    assert result.status == "needs_input"
    assert result.question_revision == 1
    assert [q.id for q in result.open_questions] == ["q1", "q2"]
    assert result.open_questions[0].default == "lru"


def test_malformed_result_raises_for_the_repair_path(repo: Path) -> None:
    with pytest.raises(ResultInvalid):
        run(FakeAdapter(Scenario(malformed_result=True)), repo)


def test_usage_and_stderr_are_passed_through(repo: Path) -> None:
    result, proc, _ = run(
        FakeAdapter(Scenario(usage={"tokens_in": 10, "tokens_out": 4}, stderr="warn")), repo
    )
    assert result.usage is not None
    assert (result.usage.tokens_in, result.usage.tokens_out) == (10, 4)
    assert "warn" in proc.stderr


# --- hanging and cancellation -----------------------------------------------------------------


def test_a_hanging_agent_times_out_and_is_killed(repo: Path) -> None:
    adapter = FakeAdapter(Scenario(hang=True, write={"partial.txt": "p\n"}))
    ctx = context(repo)
    # Include cold interpreter/schema startup on slower hosts before the deliberate hang.
    proc = run_invocation(adapter.build_invocation(task(), ctx), timeout_s=5)
    assert proc.timed_out
    assert proc.exit_code is None
    assert (repo / "worktree" / "partial.txt").exists()  # edits made before hanging remain
    result = asyncio.run(adapter.parse_result(proc, task(), ctx))
    assert result.status == "failed"  # synthesized: a timeout is never `done`


def test_cancel_kills_the_handle() -> None:
    class Handle:
        killed = False

        def kill_tree(self) -> None:
            self.killed = True

    handle = Handle()
    adapter = FakeAdapter()
    asyncio.run(adapter.cancel(handle))
    assert handle.killed
    assert adapter.cancelled == [handle]


# --- scripting per node and attempt -----------------------------------------------------------


def test_scenarios_follow_attempts_and_the_last_one_repeats(repo: Path) -> None:
    adapter = FakeAdapter().script(
        "n1", Scenario(status="failed", exit_code=1), Scenario(summary="second")
    )
    statuses = [run(adapter, repo, attempt=n)[0].status for n in (1, 2, 3)]
    assert statuses == ["failed", "done", "done"]
    assert run(adapter, repo, attempt=3)[0].summary == "second"


def test_scenarios_are_selected_by_node_with_a_default(repo: Path) -> None:
    adapter = FakeAdapter({"n1": Scenario(summary="one"), "*": Scenario(summary="other")})
    assert run(adapter, repo, node="n1")[0].summary == "one"
    assert run(adapter, repo, node="n9")[0].summary == "other"


def test_a_callable_can_choose_the_scenario(repo: Path) -> None:
    def pick(spec: TaskSpec, ctx: RunContext) -> Scenario:
        return Scenario(summary=f"{spec.goal} #{ctx.attempt}")

    assert run(FakeAdapter(pick), repo)[0].summary == "Do the thing #1"


def test_scenario_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        Scenario.model_validate({"wirte": {}})


def test_invocation_type_is_frozen() -> None:
    inv = Invocation(cmd="x", args=(), cwd=Path("."))
    with pytest.raises(AttributeError):
        inv.cmd = "y"  # type: ignore[misc]
