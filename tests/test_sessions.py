"""Durable sessions, ownership and idempotent approval-to-run intent (task 2.21)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from relay.core import sessions as sessions_module
from relay.core.events import read_events
from relay.core.platform.locks import LockProbe
from relay.core.runlock import RunLock
from relay.core.sessions import (
    SessionBusy,
    SessionConflict,
    SessionOwner,
    SessionOwnershipUnavailable,
    SessionStore,
    protected_run_ids,
    session_guard,
    session_lock_path,
)
from relay.core.store import RunStore, runs_dir

GRAPH = {"nodes": [{"id": "n1", "category": "implement", "goal": "do"}]}


def _no_locks(_directory: Path) -> LockProbe:
    return LockProbe(False, "no locks here")


def new_session(tmp_path: Path) -> SessionStore:
    return SessionStore.create(tmp_path)


def approved(owner: SessionOwner, key: str = "k1"):  # type: ignore[no-untyped-def]
    revision = owner.save_plan(GRAPH, explanation="plan")
    digest = owner.store.plan(revision)["digest"]
    return owner.approve(
        revision, digest, idempotency_key=key, expected_session_revision=owner.record.revision
    )


def child(tmp_path: Path, session_id: str, body: str) -> subprocess.CompletedProcess[str]:
    code = textwrap.dedent(
        f"""
        import os, sys
        from pathlib import Path
        from relay.core.sessions import SessionOwner, SessionBusy
        repo, sid = Path({str(tmp_path)!r}), {session_id!r}
        """
    ) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


# --- creation, ids, ownership -----------------------------------------------------------------


def test_create_reserves_a_permanent_lock_object(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    assert session_lock_path(tmp_path, store.session_id).is_file()
    assert store.read().state == "idle" and store.read().revision == 1
    assert SessionStore.list_sessions(tmp_path) == [store.session_id]
    other = new_session(tmp_path)
    assert other.session_id != store.session_id


def test_only_one_owner_at_a_time(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    with store.acquire_owner():
        with pytest.raises(SessionBusy):
            store.acquire_owner()
        result = child(
            tmp_path,
            store.session_id,
            """
            try:
                SessionOwner.acquire(repo, sid)
                print("OWNED")
            except SessionBusy:
                print("BUSY")
            """,
        )
        assert result.stdout.strip() == "BUSY", result.stderr
    # a read-only view never needs ownership
    assert store.read().session_id == store.session_id
    with store.acquire_owner():
        pass


def test_clean_close_is_idle_and_owner_death_is_interrupted(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    with store.acquire_owner() as owner:
        assert owner.record.state == "active"
    assert store.read().state == "idle"

    died = child(
        tmp_path,
        store.session_id,
        """
        owner = SessionOwner.acquire(repo, sid)
        os._exit(0)
        """,
    )
    assert died.returncode == 0, died.stderr
    assert store.read().state == "active"  # the dead owner never closed
    with store.acquire_owner() as owner:
        assert owner.record.state == "interrupted"
        types = [e.type for e in read_events(store.events_path)]
        assert "session.interrupted" in types
        owner.mark_active()
        assert owner.record.state == "active"


def test_unsupported_locking_disables_ownership_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = new_session(tmp_path)
    monkeypatch.setattr(sessions_module, "probe_lock_support", _no_locks)
    with pytest.raises(SessionOwnershipUnavailable):
        store.acquire_owner()
    run = RunStore.create(tmp_path)
    RunLock.acquire(tmp_path, run.run_id).release()  # legacy headless ownership unchanged


# --- approval and launch ----------------------------------------------------------------------


def test_approval_is_idempotent_and_binds_revision_and_digest(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    with store.acquire_owner() as owner:
        intent = approved(owner)
        again = owner.approve(
            intent.plan_revision,
            intent.approval_digest,
            idempotency_key="k1",
            expected_session_revision=1,
        )
        assert again == intent
        assert len(store.approvals()) == 1
        with pytest.raises(SessionConflict):  # same key, different content
            owner.approve(
                9, intent.approval_digest, idempotency_key="k1", expected_session_revision=1
            )


def test_stale_superseded_and_mismatched_approvals_are_rejected(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    with store.acquire_owner() as owner:
        first = owner.save_plan(GRAPH, explanation="v1")
        digest = owner.store.plan(first)["digest"]
        with pytest.raises(SessionConflict, match="stale"):
            owner.approve(first, digest, idempotency_key="a", expected_session_revision=99)
        with pytest.raises(SessionConflict, match="digest"):
            owner.approve(
                first,
                "0" * 64,
                idempotency_key="b",
                expected_session_revision=owner.record.revision,
            )
        second = owner.save_plan(GRAPH, explanation="v2", parent=first)
        assert second == first + 1
        with pytest.raises(SessionConflict, match="superseded"):
            owner.approve(
                first, digest, idempotency_key="c", expected_session_revision=owner.record.revision
            )
        owner.close_admission()
        new_digest = owner.store.plan(second)["digest"]
        with pytest.raises(SessionConflict, match="closed"):
            owner.approve(
                second,
                new_digest,
                idempotency_key="d",
                expected_session_revision=owner.record.revision,
            )


def test_start_run_admits_exactly_one_run(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    with store.acquire_owner() as owner:
        intent = approved(owner)
        first = owner.start_run()
        second = owner.start_run()
        assert first.run_id == second.run_id == intent.run_id
        assert RunStore.list_runs(tmp_path) == [intent.run_id]
        record = first.read_run()
        assert record.session is not None and record.session.session_id == store.session_id
        assert owner.record.linked_runs == [intent.run_id]
        assert owner.record.intent is not None and owner.record.intent.state == "linked"


def test_crash_after_run_creation_attaches_instead_of_duplicating(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    with store.acquire_owner() as owner:
        intent = approved(owner)
        owner.start_run()
        # Simulate dying after the run exists but before the intent was marked linked.
        record = owner.record
        assert record.intent is not None
        record.intent.state = "pending"
        record.linked_runs = []
        store._write(record)  # pyright: ignore[reportPrivateUsage]
    with store.acquire_owner() as owner:
        assert owner.record.intent is not None and owner.record.intent.state == "linked"
        assert owner.record.linked_runs == [intent.run_id]
        assert RunStore.list_runs(tmp_path) == [intent.run_id]
        assert owner.start_run().run_id == intent.run_id


def test_superseded_plan_cannot_start(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    with store.acquire_owner() as owner:
        approved(owner)
        owner.save_plan(GRAPH, explanation="revised")
        with pytest.raises(SessionConflict, match="superseded"):
            owner.start_run()
        assert RunStore.list_runs(tmp_path) == []


def test_closed_gate_blocks_start(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    with store.acquire_owner() as owner:
        approved(owner)
        owner.close_admission()
        with pytest.raises(SessionConflict, match="closed"):
            owner.start_run()
        owner.open_admission()
        owner.start_run()


# --- messages and events ----------------------------------------------------------------------


def test_messages_are_append_only_and_events_carry_no_content(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    with store.acquire_owner() as owner:
        owner.append_message("operator", "secret prompt text", provenance="operator-tty")
        owner.append_message("coordinator", "reply", provenance="coordinator")
        approved(owner)
    messages = store.messages()
    assert [m["message_id"] for m in messages] == ["m1", "m2"]
    assert messages[0]["content_hash"] != messages[1]["content_hash"]
    assert "secret prompt text" not in store.events_path.read_text(encoding="utf-8")


# --- linked-run commands and retention --------------------------------------------------------


def test_session_guard_for_linked_and_unlinked_runs(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    plain = RunStore.create(tmp_path)
    with session_guard(tmp_path, plain.run_id):  # no link: legacy behaviour
        pass
    with store.acquire_owner() as owner:
        approved(owner)
        linked = owner.start_run()
        with session_guard(tmp_path, linked.run_id, held=owner):  # the owner itself
            pass
        with pytest.raises(SessionBusy), session_guard(tmp_path, linked.run_id):
            pass  # another process (here: same process, second handle) is refused
    with (
        session_guard(tmp_path, linked.run_id),
        pytest.raises(SessionBusy),
    ):  # unowned: takes the lock
        store.acquire_owner()
    store.acquire_owner().close()  # released afterwards


def test_protected_runs_cover_linked_and_reserved_runs(tmp_path: Path) -> None:
    store = new_session(tmp_path)
    plain = RunStore.create(tmp_path)
    with store.acquire_owner() as owner:
        intent = approved(owner)
        assert protected_run_ids(tmp_path) == {intent.run_id}  # reserved, not yet created
        owner.start_run()
    assert protected_run_ids(tmp_path) == {intent.run_id}
    assert plain.run_id not in protected_run_ids(tmp_path)
    assert (runs_dir(tmp_path) / intent.run_id).is_dir()


# --- run lock integration, retention through the CLI -------------------------------------------


def test_run_lock_takes_and_releases_the_session_lock_for_linked_runs(tmp_path: Path) -> None:
    from relay.core.runlock import RunLockSessionBusy

    store = new_session(tmp_path)
    with store.acquire_owner() as owner:
        approved(owner)
        linked = owner.start_run()
        # the owner itself passes its own session through; the lock is not retaken
        RunLock.acquire(tmp_path, linked.run_id, session_owner=owner).release()
        with pytest.raises(RunLockSessionBusy):
            RunLock.acquire(tmp_path, linked.run_id)  # a second process-handle is refused
    # unowned: the command takes the session lock with the run lock and drops both together
    lock = RunLock.acquire(tmp_path, linked.run_id)
    with pytest.raises(SessionBusy):
        store.acquire_owner()
    lock.release()
    store.acquire_owner().close()


def test_unlinked_runs_keep_legacy_lock_behaviour(tmp_path: Path) -> None:
    plain = RunStore.create(tmp_path)
    store = new_session(tmp_path)
    with store.acquire_owner():
        RunLock.acquire(tmp_path, plain.run_id).release()  # an unrelated session is irrelevant


def test_clean_purge_never_removes_runs_referenced_by_a_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import init_repo
    from relay.cli.exitcodes import Exit
    from relay.cli.main import main
    from relay.core import registry as registry_module

    monkeypatch.setattr(registry_module, "USER_CONFIG", tmp_path / "no-user-config.yaml")
    repo = init_repo(tmp_path / "repo")
    store = SessionStore.create(repo)
    with store.acquire_owner() as owner:
        approved(owner)
        linked = owner.start_run()
    other = RunStore.create(repo)

    code = main(["-C", str(repo), "clean", linked.run_id, "--purge-runs", "--yes", "--json"])
    assert code == Exit.FAILURE
    assert "retained session" in capsys.readouterr().err
    assert linked.run_dir.is_dir()

    assert main(["-C", str(repo), "clean", "--purge-runs", "--yes", "--json"]) == Exit.OK
    assert linked.run_dir.is_dir() and not other.run_dir.exists()


def test_session_deletion_is_refused_while_a_run_branch_is_checked_out(tmp_path: Path) -> None:
    """A run branch checked out in a worktree outside the deletion blocks it, untouched."""
    from harness import git, init_repo
    from relay.core.sessions import DELETING, delete_session, plan_session_deletion

    repo = init_repo(tmp_path / "repo")
    store = SessionStore.create(repo)
    with store.acquire_owner() as owner:
        approved(owner)
        linked = owner.start_run()
    linked.update_run(lambda record: setattr(record, "state", "done"))  # a finished round
    head = git(repo, "rev-parse", "HEAD")
    branch = f"refs/heads/relay/{linked.run_id}/integration"
    git(repo, "update-ref", branch, head)
    elsewhere = tmp_path / "elsewhere"
    git(repo, "worktree", "add", "-q", str(elsewhere), f"relay/{linked.run_id}/integration")

    plan = plan_session_deletion(repo, store.session_id, worktree_root="auto")
    assert any("checked out" in problem and branch in problem for problem in plan.problems)
    (run,) = plan.runs
    assert run.blocked_refs == (branch,) and branch not in run.refs
    with pytest.raises(SessionConflict, match="checked out"):
        delete_session(repo, store.session_id, include_undelivered=True, worktree_root="auto")
    assert git(repo, "rev-parse", branch) == head
    assert linked.run_dir.is_dir() and not (store.dir / DELETING).exists()
    assert store.session_id in SessionStore.list_sessions(repo)

    git(repo, "worktree", "remove", "--force", str(elsewhere))
    delete_session(repo, store.session_id, include_undelivered=True, worktree_root="auto")
    assert not linked.run_dir.exists()
    assert git(repo, "for-each-ref", "--format=%(refname)", branch) == ""
