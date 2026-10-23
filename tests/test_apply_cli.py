"""CLI and installation tests for ``relay apply`` (design sections 8.3, 8.6 and 14, task 1.24b).

Every test builds a real temp git repository and a run store whose integration commit is a real
commit, then drives ``relay apply`` through :func:`relay.cli.main.main`. Integration acceptance
is a small Python one-liner, so no network or real agent CLI is involved.

Covered here: fast-forward and merge delivery with working-tree/index installation, preserved
disjoint dirty edits, refusals (dirty overlap, conflict, verification failure, declined or
unavailable confirmation, target moved) that leave the branch untouched, ``--branch`` without
moving the current checkout, the no-op case, exit codes, the ``apply.*`` events and the stable
``--json`` shape.
"""

from __future__ import annotations

import json
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Literal

import pytest

from harness import git, init_repo
from relay.cli.commands import apply as apply_cli
from relay.cli.exitcodes import Exit
from relay.cli.main import main
from relay.core.events import Event, EventLog, read_events
from relay.core.gitbase import read_ref
from relay.core.publish import integration_branch
from relay.core.specs.graph import Graph, GraphNode
from relay.core.specs.task import Acceptance, Commands, RunCheck, ScopeCheck
from relay.core.store import RunStore

RUN = "2026-10-22-a1b2"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def commit_in_worktree(
    repo: Path,
    tmp_path: Path,
    parent: str,
    files: dict[str, str | None],
    *,
    message: str = "change",
) -> str:
    """Create a commit on top of ``parent`` in a throwaway worktree; returns its sha."""
    worktree = Path(tempfile.mkdtemp(prefix="wt-", dir=tmp_path))
    git(repo, "worktree", "add", "--detach", str(worktree), parent)
    try:
        for name, content in files.items():
            target = worktree / name
            if content is None:
                target.unlink()
            else:
                write(target, content)
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", message)
        return git(worktree, "rev-parse", "HEAD")
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree))
        git(repo, "worktree", "prune")


def py_check(path: str) -> str:
    """A passing acceptance command that proves the prepared tree contains ``path``."""
    code = f"import pathlib, sys; sys.exit(0 if pathlib.Path({path!r}).is_file() else 1)"
    return shlex.join([sys.executable, "-c", code])


def py_command(exit_code: int) -> str:
    return shlex.join([sys.executable, "-c", f"raise SystemExit({exit_code})"])


def make_graph(
    *,
    acceptance: list[Acceptance] | None = None,
    commands: Commands | None = None,
    verify: Literal["default", "none"] = "default",
    reason: str | None = None,
) -> Graph:
    return Graph(
        nodes=[GraphNode(id="n1", category="implement", goal="do the thing")],
        integration_acceptance=acceptance or [],
        commands=commands,
        verify=verify,
        verification_skip_reason=reason,
    )


def create_run(
    repo: Path,
    *,
    base: str,
    integration: str | None,
    state: str = "done",
    graph: Graph | None = None,
) -> RunStore:
    """Write a run store with a recorded state, graph and the integration branch."""
    store = RunStore.create(repo, run_id=RUN)
    record = store.read_run()
    record.state = state  # type: ignore[assignment]
    if state == "done":
        record.final_commit = integration
    record.graph = graph
    store.write_run(record)
    with EventLog(store.events_path) as log:
        log.append(
            "run.started",
            {"config_hash": "test", "base_commit": base, "allow_dirty": False},
        )
        log.append(
            "run.finished",
            {"state": state, "integration_commit": integration},
        )
    if integration is not None:
        git(repo, "update-ref", integration_branch(RUN), integration)
    return store


def snapshot(repo: Path) -> tuple[str, str, str]:
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "rev-parse", "refs/heads/main"),
        git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
    )


def worktree_count(repo: Path) -> int:
    listed = git(repo, "worktree", "list", "--porcelain")
    return len([line for line in listed.splitlines() if line.startswith("worktree ")])


def tracked_status(repo: Path) -> list[str]:
    """Porcelain status without the untracked run store (``.agents/`` is runtime state)."""
    lines = git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    return [line for line in lines if ".agents" not in line]


def apply_events(store: RunStore) -> list[Event]:
    return [event for event in read_events(store.events_path) if event.type.startswith("apply.")]


def invoke(repo: Path, *args: str) -> int:
    return main(["-C", str(repo), "apply", RUN, *args])


def _decline(*_args: str) -> str:
    return "n"


def _accept(*_args: str) -> str:
    return "yes"


# --- successful delivery -----------------------------------------------------------------------


def test_fast_forward_updates_ref_and_working_tree(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    store = create_run(
        repo,
        base=base,
        integration=integration,
        graph=make_graph(acceptance=[RunCheck(run=py_check("feature.txt"))]),
    )

    code = invoke(repo, "--yes")

    assert code == Exit.OK
    assert "applied" in capsys.readouterr().out
    assert read_ref(repo, "refs/heads/main") == integration
    assert head(repo) == integration
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert tracked_status(repo) == []
    assert worktree_count(repo) == 1

    events = apply_events(store)
    assert [event.type for event in events] == [
        "apply.started",
        "apply.progress",
        "apply.progress",
        "apply.progress",
        "apply.progress",
        "apply.progress",
        "apply.finished",
    ]
    assert events[0].data == {
        "op_id": events[0].data["op_id"],
        "target": "refs/heads/main",
        "commit": integration,
    }
    assert [event.data.get("step") for event in events[1:-1]] == [
        "prepared",
        "verification-passed",
        "confirmed",
        "ref-updated",
        "installed",
    ]
    assert events[-1].data["status"] == "applied"


def test_real_merge_installs_both_sides(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"integration.txt": "run\n"})
    write(repo / "target.txt", "target\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "target work")
    target_tip = head(repo)
    create_run(
        repo,
        base=base,
        integration=integration,
        graph=make_graph(
            acceptance=[
                RunCheck(run=py_check("target.txt")),
                RunCheck(run=py_check("integration.txt")),
            ]
        ),
    )

    code = invoke(repo, "--yes", "--json")

    assert code == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "merge"
    assert payload["target"] == "refs/heads/main"
    prepared = payload["prepared_commit"]
    assert prepared not in (target_tip, integration)
    assert read_ref(repo, "refs/heads/main") == prepared
    assert head(repo) == prepared
    assert (repo / "target.txt").read_text(encoding="utf-8") == "target\n"
    assert (repo / "integration.txt").read_text(encoding="utf-8") == "run\n"
    parents = set(git(repo, "show", "-s", "--format=%P", prepared).split())
    assert parents == {target_tip, integration}
    assert tracked_status(repo) == []


def test_disjoint_dirty_edits_are_preserved(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(
        repo,
        base=base,
        integration=integration,
        graph=make_graph(acceptance=[RunCheck(run=py_check("feature.txt"))]),
    )
    write(repo / "README.md", "# staged user edit\n")
    git(repo, "add", "README.md")
    write(repo / "notes.txt", "untracked user file\n")

    code = invoke(repo, "--yes")

    assert code == Exit.OK
    assert (repo / "README.md").read_text(encoding="utf-8") == "# staged user edit\n"
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "untracked user file\n"
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    status = git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    assert "M  README.md" in status
    assert "?? notes.txt" in status
    assert not any("feature.txt" in line for line in status)


def test_branch_is_created_without_moving_the_checkout(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(
        repo,
        base=base,
        integration=integration,
        graph=make_graph(acceptance=[RunCheck(run=py_check("feature.txt"))]),
    )
    before = snapshot(repo)

    code = invoke(repo, "--branch", "feature", "--yes", "--json")

    assert code == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == "refs/heads/feature"
    assert payload["worktree_installed"] is False
    assert read_ref(repo, "refs/heads/feature") == integration
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert snapshot(repo) == before


def test_graph_build_test_lint_fallback_is_used(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(
        repo,
        base=base,
        integration=integration,
        graph=make_graph(commands=Commands(test=py_check("feature.txt"))),
    )

    code = invoke(repo, "--yes", "--json")

    assert code == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["verification"] == {"outcome": "passed", "checks": 3}
    assert read_ref(repo, "refs/heads/main") == integration


def test_verify_none_is_reported_as_skipped(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(
        repo,
        base=base,
        integration=integration,
        graph=make_graph(
            acceptance=[RunCheck(run=py_command(1))],
            verify="none",
            reason="integration covered by the run",
        ),
    )

    code = invoke(repo, "--yes", "--json")

    assert code == Exit.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["verification"]["outcome"] == "skipped"
    assert read_ref(repo, "refs/heads/main") == integration


def test_second_apply_is_a_noop_without_confirmation(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(repo, base=base, integration=integration)
    assert invoke(repo, "--yes") == Exit.OK
    capsys.readouterr()

    # No --yes here: a no-op needs no confirmation because it changes nothing.
    code = invoke(repo)

    assert code == Exit.OK
    assert "already contained" in capsys.readouterr().out
    assert read_ref(repo, "refs/heads/main") == integration


# --- refusals that leave the branch and checkout alone -----------------------------------------


def test_dirty_overlap_exits_failure_and_changes_nothing(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "run\n"})
    store = create_run(repo, base=base, integration=integration)
    write(repo / "feature.txt", "user\n")
    before = snapshot(repo)

    code = invoke(repo, "--yes")

    assert code == Exit.FAILURE
    assert "feature.txt" in capsys.readouterr().err
    assert snapshot(repo) == before
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "user\n"
    assert read_ref(repo, "refs/heads/main") == base
    events = apply_events(store)
    assert events[-1].type == "apply.finished"
    assert events[-1].data["status"] == "refused"


def test_directory_file_collision_is_a_dirty_overlap(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"docs/readme.md": "delivered\n"})
    create_run(repo, base=base, integration=integration)
    write(repo / "docs", "user file where the run needs a directory\n")
    before = snapshot(repo)

    code = invoke(repo, "--yes")

    assert code == Exit.FAILURE
    assert "docs" in capsys.readouterr().err
    assert snapshot(repo) == before
    assert (repo / "docs").read_text(encoding="utf-8").startswith("user file")


def test_merge_conflict_exits_failure_and_changes_nothing(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo / "conflict.txt", "base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add conflict file")
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"conflict.txt": "integration side\n"})
    write(repo / "conflict.txt", "target side\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "target side")
    target_tip = head(repo)
    create_run(repo, base=base, integration=integration)
    before = snapshot(repo)

    code = invoke(repo, "--yes")

    assert code == Exit.FAILURE
    assert "conflict.txt" in capsys.readouterr().err
    assert snapshot(repo) == before
    assert read_ref(repo, "refs/heads/main") == target_tip
    assert worktree_count(repo) == 1


def test_verification_failure_exits_verification_failed(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    store = create_run(
        repo,
        base=base,
        integration=integration,
        graph=make_graph(acceptance=[RunCheck(run=py_command(1))]),
    )
    before = snapshot(repo)

    code = invoke(repo, "--yes")

    assert code == Exit.VERIFICATION_FAILED
    error = capsys.readouterr().err
    assert "integration verification failed" in error
    assert snapshot(repo) == before
    assert read_ref(repo, "refs/heads/main") == base
    assert worktree_count(repo) == 1
    events = apply_events(store)
    assert events[-1].data["status"] == "refused"
    assert events[-1].data["error"] == "integration verification failed"


def test_scope_violation_fails_verification(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(
        repo,
        base=base,
        integration=integration,
        graph=make_graph(acceptance=[ScopeCheck(diff_scope=["other/**"])]),
    )

    code = invoke(repo, "--yes")

    assert code == Exit.VERIFICATION_FAILED
    assert "scope" in capsys.readouterr().err
    assert read_ref(repo, "refs/heads/main") == base


def test_non_interactive_without_yes_exits_approval_required(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    store = create_run(repo, base=base, integration=integration)
    before = snapshot(repo)
    monkeypatch.setattr(apply_cli, "_interactive", lambda: False)

    code = invoke(repo)

    assert code == Exit.APPROVAL_REQUIRED
    err = capsys.readouterr().err
    assert f"relay apply {RUN} --yes" in err
    assert snapshot(repo) == before
    assert read_ref(repo, "refs/heads/main") == base
    assert apply_events(store)[-1].data["status"] == "refused"


def test_declined_prompt_exits_approval_required(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    store = create_run(repo, base=base, integration=integration)
    before = snapshot(repo)
    monkeypatch.setattr(apply_cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", _decline)

    code = invoke(repo)

    assert code == Exit.APPROVAL_REQUIRED
    assert "not confirmed" in capsys.readouterr().err
    assert snapshot(repo) == before
    assert read_ref(repo, "refs/heads/main") == base
    assert apply_events(store)[-1].data["status"] == "refused"


def test_accepting_the_prompt_applies(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(repo, base=base, integration=integration)
    monkeypatch.setattr(apply_cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", _accept)

    code = invoke(repo)

    assert code == Exit.OK
    assert capsys.readouterr().out.startswith(f"apply run {RUN} to refs/heads/main")
    assert read_ref(repo, "refs/heads/main") == integration


def test_target_moved_while_confirming_exits_approval_required(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    store = create_run(repo, base=base, integration=integration)
    monkeypatch.setattr(apply_cli, "_interactive", lambda: True)

    def move_then_confirm(*_args: object) -> str:
        write(repo / "moved.txt", "moved\n")
        git(repo, "add", "moved.txt")
        git(repo, "commit", "-q", "-m", "moved during confirmation")
        return "y"

    monkeypatch.setattr("builtins.input", move_then_confirm)

    code = invoke(repo)

    assert code == Exit.APPROVAL_REQUIRED
    err = capsys.readouterr().err
    assert "no longer applies" in err
    assert read_ref(repo, "refs/heads/main") == head(repo)
    assert read_ref(repo, "refs/heads/main") != integration
    assert not (repo / "feature.txt").exists()
    assert apply_events(store)[-1].data["status"] == "refused"


# --- invalid input -----------------------------------------------------------------------------


def test_missing_run_exits_invalid(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = invoke(repo, "--yes")

    assert code == Exit.INVALID
    assert RUN in capsys.readouterr().err


def test_incomplete_run_exits_invalid(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(repo, base=base, integration=integration, state="running")
    before = snapshot(repo)

    code = invoke(repo, "--yes")

    assert code == Exit.INVALID
    assert "not 'done'" in capsys.readouterr().err
    assert snapshot(repo) == before


def test_force_is_rejected_in_git_mode(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(repo, base=base, integration=integration)
    before = snapshot(repo)

    code = invoke(repo, "--force", "--yes")

    assert code == Exit.INVALID
    assert "--force" in capsys.readouterr().err
    assert snapshot(repo) == before


# --- output and events -------------------------------------------------------------------------


def test_json_shape_and_event_fields(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    store = create_run(
        repo,
        base=base,
        integration=integration,
        graph=make_graph(acceptance=[RunCheck(run=py_check("feature.txt"))]),
    )

    assert invoke(repo, "--yes", "--json") == Exit.OK

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "status",
        "run_id",
        "op_id",
        "target",
        "target_tip",
        "integration_commit",
        "prepared_commit",
        "mode",
        "changed_paths",
        "verification",
        "ref_updated",
        "worktree_installed",
    }
    assert payload["status"] == "applied"
    assert payload["run_id"] == RUN
    assert payload["mode"] == "fast-forward"
    assert payload["changed_paths"] == ["feature.txt"]
    assert payload["verification"] == {"outcome": "passed", "checks": 3}
    assert payload["ref_updated"] is True
    assert payload["worktree_installed"] is True

    for event in apply_events(store):
        assert set(event.data) <= {"op_id", "target", "commit", "step", "status", "error"}
        assert event.seq >= 1


def test_json_error_shape(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = invoke(repo, "--yes", "--json")

    assert code == Exit.INVALID
    payload = json.loads(capsys.readouterr().err)
    assert set(payload) == {"error"}
