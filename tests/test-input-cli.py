"""Foreground input protocol through the CLI and a real fake worker (task 1.21)."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from harness import git, init_repo
from relay.adapters.base import AgentAdapter
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.cli.main import main
from relay.core import run as core_run
from relay.core.answers import read_answers
from relay.core.events import read_events
from relay.core.registry import Config
from relay.core.runlock import RunLock
from relay.core.specs import ResultSpec, TaskSpec, load_spec
from relay.core.store import RunStore
from test_run import config, spec

RUN = "2026-10-23-abcd"
QUESTION = {
    "id": "q1",
    "text": "Which color?",
    "kind": "choice",
    "options": ["red", "blue"],
    "default": "blue",
}


@pytest.fixture
def blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FakeAdapter, RunStore]:
    repo = init_repo(tmp_path / "repo")
    adapter = FakeAdapter(
        [
            Scenario(
                status="needs_input",
                questions=[QUESTION],
                write={"draft.txt": "draft"},
                session_id="fake-session",
            ),
            Scenario(write={"feature.txt": "blue"}),
        ]
    )

    def adapters(config: Config) -> dict[str, AgentAdapter]:
        return {"fake": adapter}

    monkeypatch.setattr(core_run, "_default_adapters", adapters)
    outcome = asyncio.run(
        core_run.run_foreground(
            repo,
            spec(max_attempts=2),
            config=config(),
            run_id=RUN,
            non_interactive=True,
        )
    )
    assert outcome.exit_code == 6
    store = RunStore.open(repo, RUN)
    assert store.read_run().state == "blocked"
    assert store.read_run().nodes["n1"].attempts == 1
    assert store.read_run().final_commit is None
    assert load_spec(ResultSpec, store.node_dir("n1") / "result.spec.yaml").question_revision == 1
    return repo, adapter, store


def reply(repo: Path, *extra: str) -> int:
    return main(["-C", str(repo), "reply", RUN, "n1", *extra])


def test_reply_resume_preserves_edits_and_changes_input_key(
    blocked: tuple[Path, FakeAdapter, RunStore],
) -> None:
    repo, adapter, store = blocked
    original_key = store.read_run().nodes["n1"].input_key
    assert reply(repo, "blue", "--yes", "--resume", "--json") == 0
    record = store.read_run()
    assert record.nodes["n1"].attempts == 2
    assert record.nodes["n1"].input_key != original_key
    assert record.nodes["n1"].blocked_reason is None
    assert git(repo, "show", f"{record.final_commit}:draft.txt") == "draft"
    assert git(repo, "show", f"{record.final_commit}:feature.txt") == "blue"
    assert len(adapter.invocations) == 2
    assert "operator-cli" in (adapter.invocations[-1].stdin or "")
    assert "Which color?" in (adapter.invocations[-1].stdin or "")
    observed = json.loads((store.node_dir("n1") / "fake-observed.json").read_text())
    assert observed["resume_session"] is None
    assert (store.node_dir("n1") / "attempts/01/result.spec.yaml").is_file()
    events = list(read_events(store.events_path))
    assert any(event.type == "input.recorded" for event in events)
    assert all("answers" not in event.data for event in events)
    assert any(
        event.type == "node.routed" and event.data["decision"]["resumed"] for event in events
    )


@pytest.mark.parametrize("arguments", [("green", "--yes"), ("blue", "--revision", "2", "--yes")])
def test_invalid_reply_changes_nothing(
    blocked: tuple[Path, FakeAdapter, RunStore], arguments: tuple[str, ...]
) -> None:
    repo, _, store = blocked
    before = store.run_json.read_bytes(), store.events_path.read_bytes()
    assert reply(repo, *arguments) == 2
    assert not (store.node_dir("n1") / "input_answers.yaml").exists()
    assert before == (store.run_json.read_bytes(), store.events_path.read_bytes())


def test_confirmation_changes_nothing(blocked: tuple[Path, FakeAdapter, RunStore]) -> None:
    repo, _, store = blocked
    before = store.events_path.read_bytes()
    assert reply(repo, "blue") == 7
    assert not (store.node_dir("n1") / "input_answers.yaml").exists()
    assert store.events_path.read_bytes() == before


def test_idempotency_and_conflicting_submission(
    blocked: tuple[Path, FakeAdapter, RunStore],
) -> None:
    repo, _, store = blocked
    assert reply(repo, "blue", "--yes", "--submission-id", "fake-submission") == 0
    before = store.events_path.read_bytes()
    assert reply(repo, "blue", "--yes", "--submission-id", "fake-submission") == 0
    assert before == store.events_path.read_bytes()
    assert reply(repo, "red", "--yes", "--submission-id", "fake-submission") == 2
    assert len(read_answers(store.node_dir("n1"), "n1").submissions) == 1


def test_attempt_cap_requires_explicit_raise(blocked: tuple[Path, FakeAdapter, RunStore]) -> None:
    repo, adapter, store = blocked
    path = store.node_dir("n1") / "task.spec.yaml"
    task = load_spec(TaskSpec, path)
    task.budget.max_attempts = 1
    store.write_spec(path.relative_to(store.run_dir).as_posix(), task)
    assert reply(repo, "blue", "--yes", "--resume") == 6
    assert store.read_run().nodes["n1"].blocked_reason == "attempts_exhausted"
    assert len(adapter.invocations) == 1
    assert read_answers(store.node_dir("n1"), "n1").submissions
    assert main(["-C", str(repo), "resume", RUN, "--max-attempts", "2", "--non-interactive"]) == 0


def test_live_owner_refuses_reply(blocked: tuple[Path, FakeAdapter, RunStore]) -> None:
    repo, _, store = blocked
    with RunLock.acquire(repo, RUN):
        assert reply(repo, "blue", "--yes") == 1
    assert not read_answers(store.node_dir("n1"), "n1").submissions


def test_repeated_questions_increment_revision(blocked: tuple[Path, FakeAdapter, RunStore]) -> None:
    repo, adapter, store = blocked
    adapter.script("*", Scenario(status="needs_input", questions=[QUESTION]))
    assert reply(repo, "blue", "--yes", "--resume") == 6
    result = load_spec(ResultSpec, store.node_dir("n1") / "result.spec.yaml")
    assert result.question_revision == 2
    assert reply(repo, "blue", "--yes", "--revision", "1") == 2
    assert reply(repo, "red", "--yes", "--revision", "2") == 0
    assert len(read_answers(store.node_dir("n1"), "n1").submissions) == 2


@pytest.mark.parametrize("content", ['{"unknown": "blue"}', '{"q1": "blue", "q1": "red"}', "{}"])
def test_answer_mapping_validation(
    blocked: tuple[Path, FakeAdapter, RunStore], content: str
) -> None:
    repo, _, store = blocked
    path = repo.parent / "answers.json"
    path.write_text(content)
    assert reply(repo, "--answers-file", "../answers.json", "--yes") == 2
    assert not read_answers(store.node_dir("n1"), "n1").submissions


def test_exhausted_duration_preserves_answers(blocked: tuple[Path, FakeAdapter, RunStore]) -> None:
    repo, adapter, store = blocked
    path = store.node_dir("n1") / "task.spec.yaml"
    task = load_spec(TaskSpec, path)
    task.budget.max_duration_s = 1
    store.write_spec(path.relative_to(store.run_dir).as_posix(), task)
    store.update_run(lambda record: setattr(record.nodes["n1"], "active_duration_s", 2.0))
    assert reply(repo, "blue", "--yes", "--resume") == 6
    assert store.read_run().nodes["n1"].blocked_reason == "duration_exhausted"
    assert store.read_run().nodes["n1"].attempts == 1
    assert len(adapter.invocations) == 1
    assert read_answers(store.node_dir("n1"), "n1").submissions


@pytest.mark.parametrize("answer, expected", [("", 0), (":abort", 4)])
def test_tty_default_or_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, expected: int
) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("builtins.input", lambda: answer)
    repo = init_repo(tmp_path / "repo")
    adapter = FakeAdapter([Scenario(status="needs_input", questions=[QUESTION]), Scenario()])
    outcome = asyncio.run(
        core_run.run_foreground(
            repo,
            spec(max_attempts=2),
            config=config(),
            adapters={"fake": adapter},
            run_id=RUN,
            out=Terminal(),
        )
    )
    assert outcome.exit_code == expected
    if expected == 0:
        submission = read_answers(RunStore.open(repo, RUN).node_dir("n1"), "n1").submissions[0]
        assert submission.provenance == "operator-tty"
        assert submission.answers == {"q1": "blue"}


def test_json_approval_and_validation_errors(
    blocked: tuple[Path, FakeAdapter, RunStore], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = blocked
    capsys.readouterr()
    assert reply(repo, "blue", "--json") == 7
    assert "--yes" in json.loads(capsys.readouterr().err)["error"]
    assert reply(repo, "green", "--yes", "--json") == 2
    assert "invalid option" in json.loads(capsys.readouterr().err)["error"]
