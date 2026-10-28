"""Observer paths never mutate a live owner's run or session (revision log TU2, task 2.29).

`RunStore.open` and `SessionStore.open` used to sweep `.tmp-*` files, so any reader of an
active run or session (`linked_session`, `round_state`, `protected_run_ids`, `RunLock.acquire`
contention, headless `apply`, `clean`, `status`) could delete the owner's in-flight atomic
write and make its rename fail. Only owners sweep now: `SessionOwner.acquire` and
`RunLock.acquire`, after the lock is held. Headless `apply` also no longer opens (and so
repairs) the event log of a run it is about to refuse.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness import git, init_repo
from relay.cli.exitcodes import Exit
from relay.cli.main import main
from relay.core import registry as registry_module
from relay.core import run_purge
from relay.core.runlock import RunLock, RunLockSessionBusy
from relay.core.sessions import (
    SessionBusy,
    SessionOwner,
    SessionStore,
    linked_session,
    protected_run_ids,
    round_state,
    session_guard,
)
from relay.core.store import RunStore

GRAPH = {"nodes": [{"id": "n1", "category": "implement", "goal": "do"}]}
TORN = b'{"seq": 99, "type": "node.sta'  # a live writer's record, not yet complete


@pytest.fixture(autouse=True)
def isolate_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "USER_CONFIG", tmp_path / "no-user-config.yaml")


@dataclass
class Live:
    repo: Path
    owner: SessionOwner
    run: RunStore
    run_temp: Path
    session_temp: Path
    events_before: bytes


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    """A session owned by a live owner, with a linked round whose run lock is held."""
    repo = init_repo(tmp_path / "repo")
    owner = SessionStore.create(repo).acquire_owner()
    revision = owner.save_plan(GRAPH, explanation="plan")
    owner.approve(
        revision,
        owner.store.plan(revision)["digest"],
        idempotency_key="k",
        expected_session_revision=owner.record.revision,
    )
    run = owner.start_run()
    lock = RunLock.acquire(repo, run.run_id, session_owner=owner)
    run_temp = run.run_dir / ".tmp-run.json.4242.abcd"  # the owner's write, mid-rename
    run_temp.write_text("{}", encoding="utf-8")
    session_temp = owner.store.dir / ".tmp-session.json.4242.abcd"
    session_temp.write_text("{}", encoding="utf-8")
    with open(run.events_path, "ab") as handle:  # mid-append, no newline yet
        handle.write(TORN)
    try:
        yield Live(repo, owner, run, run_temp, session_temp, run.events_path.read_bytes())
    finally:
        lock.release()
        owner.close()


def untouched(live: Live) -> None:
    assert live.run_temp.exists(), "an observer swept the run owner's in-flight write"
    assert live.session_temp.exists(), "an observer swept the session owner's in-flight write"
    assert live.run.events_path.read_bytes() == live.events_before, "the event log was repaired"


def test_core_observers_never_sweep_or_write(live: Live) -> None:
    repo, run_id, sid = live.repo, live.run.run_id, live.owner.store.session_id
    assert RunStore.open(repo, run_id).read_run().session is not None
    assert SessionStore.open(repo, sid).read().linked_runs == [run_id]
    assert linked_session(repo, run_id) == sid
    assert round_state(repo, run_id) == "active"
    assert run_id in protected_run_ids(repo)
    with pytest.raises(SessionBusy), session_guard(repo, run_id):
        pass
    with pytest.raises(RunLockSessionBusy):
        RunLock.acquire(repo, run_id)  # a contender, refused before it holds anything
    with pytest.raises(SessionBusy):
        SessionOwner.acquire(repo, sid)
    untouched(live)


def test_headless_commands_never_sweep_a_live_round(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, run_id = live.repo, live.run.run_id
    assert main(["-C", str(repo), "status", run_id, "--json"]) == Exit.OK
    assert main(["-C", str(repo), "apply", run_id, "--yes", "--json"]) == Exit.FAILURE
    assert "owned by another process" in capsys.readouterr().err
    assert main(["-C", str(repo), "clean", "--json"]) == Exit.OK
    capsys.readouterr()
    assert main(["-C", str(repo), "clean", "--purge-runs", "--yes", "--json"]) == Exit.OK
    (entry,) = json.loads(capsys.readouterr().out)["runs"]
    assert entry["run_id"] == run_id and "in progress" in entry["skipped"]
    assert main(["-C", str(repo), "clean", run_id, "--purge-runs", "--yes"]) == Exit.FAILURE
    untouched(live)
    assert live.run.run_dir.is_dir()


def test_apply_of_a_live_headless_run_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = init_repo(tmp_path / "repo")
    store = RunStore.create(repo)
    lock = RunLock.acquire(repo, store.run_id)  # a live `relay run` owner
    try:
        in_flight = store.run_dir / ".tmp-run.json.77.beef"
        in_flight.write_text("{}", encoding="utf-8")
        store.events_path.write_bytes(TORN)
        assert main(["-C", str(repo), "apply", store.run_id, "--yes"]) == Exit.INVALID
        assert "running" in capsys.readouterr().err
        assert in_flight.exists()
        assert store.events_path.read_bytes() == TORN  # not truncated, no newline appended
    finally:
        lock.release()


def test_only_owners_sweep_after_taking_the_lock(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    owner = SessionStore.create(repo).acquire_owner()
    sid = owner.store.session_id
    run = RunStore.create(repo)
    run_leftover = run.run_dir / ".tmp-run.json.1.dead"
    run_leftover.write_text("{}", encoding="utf-8")
    session_leftover = owner.store.dir / ".tmp-session.json.1.dead"
    session_leftover.write_text("{}", encoding="utf-8")
    owner.close()
    RunStore.open(repo, run.run_id)
    SessionStore.open(repo, sid)
    assert run_leftover.exists() and session_leftover.exists()
    RunLock.acquire(repo, run.run_id).release()
    SessionOwner.acquire(repo, sid).close()
    assert not run_leftover.exists() and not session_leftover.exists()


# --- clean uses the shared purge rules -----------------------------------------------------------


def test_clean_keeps_the_record_when_a_ref_cannot_be_deleted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refs go before the record; a failed ref deletion keeps the record for a retry."""
    repo = init_repo(tmp_path / "repo")
    store = RunStore.create(repo)
    store.update_run(lambda r: setattr(r, "state", "done"))
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", f"refs/relay/{store.run_id}/n1", head)
    real = run_purge._git  # pyright: ignore[reportPrivateUsage]

    def refusing(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "update-ref":
            return subprocess.CompletedProcess(args, 1, "", "ref locked")
        return real(repo_root, *args)

    monkeypatch.setattr(run_purge, "_git", refusing)
    code = main(["-C", str(repo), "clean", store.run_id, "--purge-runs", "--yes", "--json"])
    assert code == Exit.FAILURE
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"dry_run", "runs", "removed", "errors"}
    entry = data["runs"][0]
    assert set(entry) == {
        "run_id",
        "state",
        "skipped",
        "note",
        "worktrees",
        "scratch",
        "run_directory",
        "refs",
        "blocked_refs",  # added for blocked run branches (TU21)
        "error",
    }
    assert "ref locked" in entry["error"] and "kept" in entry["run_directory"]["error"]
    assert store.run_dir.is_dir()  # the purge can be retried

    monkeypatch.setattr(run_purge, "_git", real)
    assert main(["-C", str(repo), "clean", store.run_id, "--purge-runs", "--yes"]) == Exit.OK
    assert not store.run_dir.exists()
    assert git(repo, "for-each-ref", f"refs/relay/{store.run_id}/") == ""
