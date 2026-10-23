"""``relay status``, ``relay result``, ``relay agents`` and ``relay clean`` (task 1.22).

Every test builds the run directory directly (no supervisor, no network and no real agent CLI),
so the inspection commands are exercised exactly as the design defines them: they read
``run.json`` and ``events.jsonl`` and never depend on the derived ``routes.jsonl`` projection.
``clean`` tests use a real temp git repository so worktree registration is real and the
destructive guarantees (main worktree, unrelated paths, live runs and lock objects) are checked
against git itself.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest

from harness import git, init_repo
from relay.cli.exitcodes import Exit
from relay.cli.main import main
from relay.core import registry as registry_module
from relay.core.events import EventLog
from relay.core.runlock import RunLock, lock_path
from relay.core.specs.result import Changes, OpenQuestion, ResultSpec, Verification, VerifyDetail
from relay.core.store import RunStore
from relay.core.worktree import WorktreeManager

RUN = "2026-10-22-a1b2"
OTHER = "2026-10-22-c3d4"
LIVE = "2026-10-22-e5f6"
NODE = "n1"
DEAD_PID = 99_999_999


@pytest.fixture(autouse=True)
def isolate_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the machine's real agents.yaml out of every test."""
    monkeypatch.setattr(registry_module, "USER_CONFIG", tmp_path / "no-user-config.yaml")


def make_run(
    repo: Path,
    *,
    run_id: str = RUN,
    state: str = "done",
    node_state: str | None = "done",
    attempts: int = 1,
    blocked_reason: str | None = None,
    output_commit: str | None = None,
    final_commit: str | None = None,
    created_at: str | None = None,
) -> RunStore:
    store = RunStore.create(repo, run_id=run_id)
    if node_state is not None:
        # `add_node` rewrites `run.json`, so every later change is applied to a fresh read.
        store.add_node(NODE)
    record = store.read_run()
    record.state = state  # type: ignore[assignment]
    if created_at is not None:
        record.created_at = created_at
    if node_state is not None:
        node = record.nodes[NODE]
        node.state = node_state  # type: ignore[assignment]
        node.attempts = attempts
        node.blocked_reason = blocked_reason
        node.output_commit = output_commit
    record.final_commit = final_commit
    store.write_run(record)
    return store


def append_events(store: RunStore, *events: tuple[str, dict[str, Any]]) -> None:
    log = EventLog(store.events_path)
    try:
        for type_, payload in events:
            log.append(type_, payload)
    finally:
        log.close()


def write_result(
    store: RunStore,
    *,
    status: Literal["done", "failed", "needs_input", "blocked", "cancelled"] = "done",
    result: ResultSpec | None = None,
    node_id: str = NODE,
    directory: str | None = None,
) -> Path:
    spec = result or ResultSpec(id=node_id, status=status, summary="did the thing")
    return store.write_spec(f"{directory or node_id}/result.spec.yaml", spec)


def call(repo: Path, *args: str) -> int:
    return main(["-C", str(repo), *args])


def payload(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out)


# --- status -----------------------------------------------------------------------------------


def test_status_single_run_json_and_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_run(
        repo,
        state="done",
        attempts=2,
        output_commit="a" * 40,
        final_commit="b" * 40,
    )
    append_events(
        store,
        (
            "run.started",
            {"config_hash": "abc", "base_commit": "c" * 40, "allow_dirty": False},
        ),
        ("run.finished", {"state": "done", "integration_commit": "b" * 40}),
    )

    assert call(repo, "status", RUN, "--json") == Exit.OK
    data = payload(capsys)
    assert data["count"] == 1
    run = data["runs"][0]
    assert run["run_id"] == RUN
    assert run["state"] == "done"
    assert run["final_commit"] == "b" * 40
    assert run["nodes"] == [
        {
            "node_id": NODE,
            "state": "done",
            "attempts": 2,
            "blocked_reason": None,
            "output_commit": "a" * 40,
            "dir": f"01-{NODE}",
        }
    ]
    assert isinstance(run["last_event_age_s"], int)

    assert call(repo, "status", RUN) == Exit.OK
    text = capsys.readouterr().out
    assert f"run {RUN}: done" in text
    assert "final integration commit" in text
    assert NODE in text


def test_status_blocked_reason_and_missing_projection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_run(repo, state="blocked", node_state="blocked", blocked_reason="needs_input")
    append_events(store, ("run.finished", {"state": "blocked"}))

    assert call(repo, "status", RUN) == Exit.OK
    text = capsys.readouterr().out
    assert "needs_input" in text

    # No routes.jsonl exists; status must not depend on the derived projection.
    assert not (store.run_dir / "routes.jsonl").exists()
    assert call(repo, "status", RUN, "--json") == Exit.OK
    assert payload(capsys)["runs"][0]["reasons"] == [f"{NODE}: needs_input", "run: blocked"]


def test_status_lists_recent_runs_newest_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    make_run(repo, run_id=RUN, created_at="2026-10-22T09:00:00.000Z")
    make_run(repo, run_id=OTHER, created_at=datetime.now(UTC).isoformat(), state="failed")

    assert call(repo, "status", "--json") == Exit.OK
    data = payload(capsys)
    assert data["count"] == 2
    assert [entry["run_id"] for entry in data["runs"]] == [OTHER, RUN]
    assert data["runs"][0]["state"] == "failed"


def test_status_missing_run_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert call(repo, "status", RUN, "--json") == Exit.INVALID
    err = capsys.readouterr().err
    assert "not found" in err
    assert "Traceback" not in err

    # A path-like id is rejected before any lookup.
    assert call(repo, "status", "../escape") == Exit.INVALID
    assert "not a valid run id" in capsys.readouterr().err


def test_status_corrupt_run_json_is_a_store_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    run_dir = repo / ".agents" / "runs" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{not json", encoding="utf-8")
    assert call(repo, "status", RUN) == Exit.FAILURE
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert "Traceback" not in err


# --- result -----------------------------------------------------------------------------------


def test_result_done_payload_and_field_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_run(repo, state="done", output_commit="a" * 40, final_commit="b" * 40)
    write_result(
        store,
        result=ResultSpec(
            id=NODE,
            status="done",
            agent="fake",
            model="fake-model",
            summary="added a file",
            changes=Changes(
                branch=f"relay/{RUN}/{NODE}",
                input_commit="c" * 40,
                output_commit="a" * 40,
                commits=["a" * 40],
                files=["src/hello.txt", "tests/test_hello.py"],
            ),
            verification=Verification(
                acceptance="passed",
                details=[VerifyDetail(run="pytest -q", exit=0)],
            ),
            open_questions=[OpenQuestion(id="q1", text="Keep this?")],
        ),
    )

    assert call(repo, "result", RUN, "--json") == Exit.OK
    data = payload(capsys)
    assert data["status"] == "done"
    assert data["result_found"] is True
    assert data["changes"]["files"] == ["src/hello.txt", "tests/test_hello.py"]
    assert data["changes"]["branch"] == f"relay/{RUN}/{NODE}"
    assert data["verification"] == {
        "mode": "default",
        "acceptance": "passed",
        "skip_reason": None,
        "details": [{"run": "pytest -q", "exit": 0}],
    }
    assert data["open_questions"][0]["id"] == "q1"
    assert data["output_commit"] == "a" * 40
    assert data["integration_commit"] == "b" * 40

    assert call(repo, "result", RUN, NODE, "changes.files") == Exit.OK
    assert json.loads(capsys.readouterr().out) == ["src/hello.txt", "tests/test_hello.py"]

    assert call(repo, "result", RUN) == Exit.OK
    text = capsys.readouterr().out
    assert "verification: passed" in text
    assert "changed files: 2" in text


def test_result_needs_input_questions_from_recorded_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_run(repo, state="blocked", node_state="blocked", blocked_reason="needs_input")
    record = store.read_run()
    write_result(
        store,
        result=ResultSpec(
            id=NODE,
            status="needs_input",
            question_revision=1,
            open_questions=[
                OpenQuestion(
                    id="q1",
                    text="Which timeout?",
                    kind="choice",
                    options=["300ms", "1s"],
                    default="300ms",
                )
            ],
        ),
        directory=record.nodes[NODE].dir,
    )

    assert call(repo, "result", RUN, "--json") == Exit.OK
    data = payload(capsys)
    assert data["status"] == "needs_input"
    assert data["open_questions"][0]["options"] == ["300ms", "1s"]
    assert data["open_questions"][0]["default"] == "300ms"


def test_result_without_projection_or_spec_is_not_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_run(repo, state="failed", node_state="failed", attempts=2)
    assert not (store.run_dir / "routes.jsonl").exists()

    assert call(repo, "result", RUN, "--json") == Exit.OK
    data = payload(capsys)
    assert data["status"] == "failed"
    assert data["result_found"] is False
    assert data["changes"] is None
    assert data["verification"] is None

    assert call(repo, "result", RUN) == Exit.OK
    assert "result spec: not written" in capsys.readouterr().out


def test_result_errors_are_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    make_run(repo)
    assert call(repo, "result", RUN, NODE, "not.a.field") == Exit.INVALID
    assert "unknown field" in capsys.readouterr().err

    assert call(repo, "result", "2026-10-22-ffff") == Exit.INVALID
    assert "not found" in capsys.readouterr().err

    assert call(repo, "result", "../escape") == Exit.INVALID
    assert "not a valid run id" in capsys.readouterr().err


# --- agents -----------------------------------------------------------------------------------


def test_agents_lists_config_without_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".agents").mkdir(parents=True)
    (repo / ".agents" / "agents.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "agents:",
                "  fake:",
                "    enabled: true",
                f"    cmd: {sys.executable!r}",
                "    categories: [implement]",
                "    models: {fast: fake-model}",
                "    env: {RELAY_TEST_SECRET: super-secret-value}",
                "  absent:",
                "    enabled: true",
                "    cmd: definitely-not-a-relay-cli",
                "    categories: [review]",
                "  disabled:",
                "    enabled: false",
                "    cmd: definitely-not-a-relay-cli",
                "    categories: [test]",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert call(repo, "agents", "--json") == Exit.OK
    data = payload(capsys)
    by_name = {entry["name"]: entry for entry in data["agents"]}
    assert data["count"] == len(data["agents"])
    assert data["count"] >= 3
    assert by_name["fake"]["available"] is True
    assert by_name["fake"]["version"]
    assert by_name["fake"]["categories"] == ["implement"]
    assert by_name["fake"]["models"] == {"fast": "fake-model"}
    assert by_name["absent"]["available"] is False
    assert by_name["absent"]["note"] == "command not found on PATH"
    assert by_name["disabled"]["available"] is None
    assert by_name["disabled"]["note"] == "agent disabled"

    # No environment value, prompt or secret may leak into the listing.
    out = capsys.readouterr().out
    assert "super-secret-value" not in out
    assert "RELAY_TEST_SECRET" not in out
    assert '"env"' not in out
    assert "pricing" not in out

    assert call(repo, "agents") == Exit.OK
    text = capsys.readouterr().out
    assert "fake" in text
    assert "definitely-not-a-relay-cli" in text
    assert "super-secret-value" not in text


def test_agents_invalid_config_is_invalid_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".agents").mkdir(parents=True)
    (repo / ".agents" / "agents.yaml").write_text("version: 1\nagents: [\n", encoding="utf-8")
    assert call(repo, "agents", "--json") == Exit.INVALID
    assert "relay agents:" in capsys.readouterr().err


# --- clean ------------------------------------------------------------------------------------


def make_worktree(repo: Path, run_id: str = RUN, node_id: str = NODE) -> Path:
    head = git(repo, "rev-parse", "HEAD")
    return WorktreeManager(repo, run_id).create(node_id, head).path


def make_scratch(repo: Path, run_id: str = RUN) -> Path:
    scratch = repo / ".agents" / "scratch" / run_id / NODE / "target"
    scratch.mkdir(parents=True)
    (scratch / "artifact").write_text("x", encoding="utf-8")
    return scratch.parent.parent


def test_clean_removes_only_relay_owned_worktrees_and_scratch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = init_repo(tmp_path / "repo")
    store = make_run(repo)
    worktree = make_worktree(repo)
    scratch = make_scratch(repo)

    # An unrelated directory and a second run's state must survive.
    unrelated = repo / ".agents" / "worktrees" / "unrelated-run" / NODE
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
    other_store = make_run(repo, run_id=OTHER)
    other_worktree = make_worktree(repo, run_id=OTHER)
    other_scratch = make_scratch(repo, run_id=OTHER)

    # A lock object is never cleaned.
    stale_lock = lock_path(repo, RUN)
    stale_lock.parent.mkdir(parents=True, exist_ok=True)
    stale_lock.write_text(f'{{"pid": {DEAD_PID}, "start": null}}\n', encoding="utf-8")
    main_readme = repo / "README.md"

    assert call(repo, "clean", RUN, "--json") == Exit.OK
    data = payload(capsys)
    assert data["runs"][0]["run_id"] == RUN
    assert data["removed"]["worktrees"] == 1
    assert data["removed"]["run_directories"] == 0

    assert not worktree.exists()
    assert not scratch.exists()
    assert store.run_dir.is_dir()
    assert (store.run_dir / "run.json").is_file()
    assert stale_lock.exists()
    assert unrelated.is_dir()
    assert (unrelated / "keep.txt").is_file()
    assert other_worktree.exists()
    assert other_scratch.exists()
    assert other_store.run_dir.is_dir()
    assert main_readme.is_file()
    worktrees = git(repo, "worktree", "list", "--porcelain")
    assert f"worktree {repo.as_posix()}" in worktrees


def test_clean_dry_run_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = init_repo(tmp_path / "repo")
    make_run(repo)
    worktree = make_worktree(repo)
    scratch = make_scratch(repo)

    assert call(repo, "clean", RUN, "--dry-run", "--json") == Exit.OK
    data = payload(capsys)
    assert data["dry_run"] is True
    assert data["runs"][0]["worktrees"][0]["removed"] is False
    assert worktree.exists()
    assert scratch.exists()
    assert (repo / ".agents" / "runs" / RUN).is_dir()


def test_clean_purge_refuses_a_live_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = init_repo(tmp_path / "repo")
    store = make_run(repo)
    lock = RunLock.acquire(repo, RUN)
    try:
        assert call(repo, "clean", RUN, "--dry-run", "--json") == Exit.OK
        assert "in progress" in str(payload(capsys)["runs"][0]["skipped"])
        assert call(repo, "clean", RUN, "--purge-runs", "--yes", "--json") == Exit.FAILURE
        err = capsys.readouterr().err
        assert "in progress" in err
        assert store.run_dir.is_dir()
        assert lock_path(repo, RUN).exists()
    finally:
        lock.release()


def test_clean_purge_requires_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = init_repo(tmp_path / "repo")
    store = make_run(repo)

    assert call(repo, "clean", RUN, "--purge-runs") == Exit.APPROVAL_REQUIRED
    err = capsys.readouterr().err
    assert "approval required" in err
    assert f"relay clean {RUN} --purge-runs --yes" in err
    assert store.run_dir.is_dir()


def test_clean_purge_removes_run_directory_and_refs_but_not_lock_objects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = init_repo(tmp_path / "repo")
    store = make_run(repo)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", f"refs/relay/{RUN}/{NODE}", head)
    git(repo, "update-ref", f"refs/heads/relay/{RUN}/integration", head)
    git(repo, "update-ref", "refs/relay/other-run/n1", head)

    stale_lock = lock_path(repo, RUN)
    stale_lock.parent.mkdir(parents=True, exist_ok=True)
    stale_lock.write_text(f'{{"pid": {DEAD_PID}, "start": null}}\n', encoding="utf-8")

    assert call(repo, "clean", RUN, "--purge-runs", "--yes", "--json") == Exit.OK
    data = payload(capsys)
    assert data["removed"]["run_directories"] == 1
    assert data["removed"]["refs"] == 2

    assert not store.run_dir.exists()
    assert stale_lock.exists()
    refs = [
        line
        for line in git(repo, "for-each-ref", "--format=%(refname)").splitlines()
        if "relay" in line
    ]
    assert refs == ["refs/relay/other-run/n1"]


def test_clean_without_run_id_skips_live_runs_and_honours_older_than(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = init_repo(tmp_path / "repo")
    make_run(repo, run_id=RUN, created_at="2020-01-01T00:00:00.000Z")
    make_run(repo, run_id=OTHER, created_at=datetime.now(UTC).isoformat())
    make_run(repo, run_id=LIVE, created_at="2020-01-01T00:00:00.000Z")
    live = RunLock.acquire(repo, LIVE)
    try:
        assert call(repo, "clean", "--older-than", "1d", "--json") == Exit.OK
        data = payload(capsys)
        by_id = {entry["run_id"]: entry for entry in data["runs"]}
        assert by_id[RUN]["skipped"] is None
        assert "newer than --older-than" in str(by_id[OTHER]["skipped"])
        assert "in progress" in str(by_id[LIVE]["skipped"])
        assert (repo / ".agents" / "runs" / OTHER).is_dir()
        assert (repo / ".agents" / "runs" / LIVE).is_dir()
    finally:
        live.release()

    assert call(repo, "clean", "--older-than", "nonsense") == Exit.INVALID
    assert "--older-than" in capsys.readouterr().err


def test_clean_missing_run_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert call(repo, "clean", RUN) == Exit.INVALID
    assert "not found" in capsys.readouterr().err
