from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from relay.core import store as store_module
from relay.core.runrecord import RunRecord
from relay.core.specs import ResultSpec
from relay.core.store import (
    RunStore,
    StoreError,
    atomic_write,
    new_run_id,
    sweep_temp_files,
)

FIXED = datetime(2026, 10, 22, 9, 41, 3, tzinfo=UTC)


def _next_of(ids: Iterator[str]) -> Callable[..., str]:
    def pick(*_args: object, **_kwargs: object) -> str:
        return next(ids)

    return pick


def _ignore_path(_path: Path) -> None:
    return None


def make(repo: Path, **kwargs: object) -> RunStore:
    return RunStore.create(repo, now=lambda: FIXED, **kwargs)  # type: ignore[arg-type]


# --- layout and run ids -----------------------------------------------------------------------


def test_create_makes_layout_and_initial_run_json(tmp_path: Path) -> None:
    store = make(tmp_path, config_hash="abc")
    assert store.run_dir.parent == tmp_path / ".agents" / "runs"
    assert store.run_dir.is_dir()
    record = store.read_run()
    assert record.run_id == store.run_id
    assert record.state == "running"
    assert record.config_hash == "abc"
    assert record.created_at == "2026-10-22T09:41:03.000Z"
    assert record.owner_generation == 0


def test_run_id_format() -> None:
    run_id = new_run_id(FIXED)
    assert run_id.startswith("2026-10-22-")
    assert len(run_id) == len("2026-10-22-a1b2")
    int(run_id[-4:], 16)


def test_run_id_date_is_utc() -> None:
    local = datetime(2026, 10, 22, 23, 30, tzinfo=UTC).astimezone()
    assert new_run_id(local).startswith("2026-10-22-")


def test_collision_draws_a_new_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = iter(["2026-10-22-aaaa", "2026-10-22-aaaa", "2026-10-22-bbbb"])
    monkeypatch.setattr(store_module, "new_run_id", _next_of(ids))
    first = make(tmp_path)
    second = make(tmp_path)
    assert (first.run_id, second.run_id) == ("2026-10-22-aaaa", "2026-10-22-bbbb")


def test_explicit_existing_id_is_an_error(tmp_path: Path) -> None:
    make(tmp_path, run_id="fixed-1")
    with pytest.raises(StoreError, match="already exists"):
        make(tmp_path, run_id="fixed-1")


def test_ids_are_not_reused_while_a_lock_object_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = tmp_path / ".agents" / "locks"
    locks.mkdir(parents=True)
    (locks / "repo123-2026-10-22-aaaa.lock").write_text("")
    ids = iter(["2026-10-22-aaaa", "2026-10-22-cccc"])
    monkeypatch.setattr(store_module, "new_run_id", _next_of(ids))
    assert make(tmp_path).run_id == "2026-10-22-cccc"
    with pytest.raises(StoreError, match="cannot be reused"):
        make(tmp_path, run_id="2026-10-22-aaaa")


def test_open_and_list_runs(tmp_path: Path) -> None:
    a = make(tmp_path)
    assert RunStore.list_runs(tmp_path) == [a.run_id]
    assert RunStore.open(tmp_path, a.run_id).run_id == a.run_id
    with pytest.raises(StoreError, match="not found"):
        RunStore.open(tmp_path, "nope")
    assert RunStore.list_runs(tmp_path / "other") == []


# --- nodes ------------------------------------------------------------------------------------


def test_nodes_get_numbered_directories(tmp_path: Path) -> None:
    store = make(tmp_path)
    one = store.add_node("n1-plan")
    two = store.add_node("n2-research")
    assert (one.name, two.name) == ("01-n1-plan", "02-n2-research")
    assert store.add_node("n1-plan") == one  # idempotent
    record = store.read_run()
    assert record.nodes["n2-research"].dir == "02-n2-research"
    assert record.nodes["n2-research"].state == "pending"
    assert store.node_dir("n1-plan") == one
    with pytest.raises(StoreError):
        store.node_dir("ghost")


# --- paths, specs and files -------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["../escape", "a/../../b", "/abs", "C:\\x" if os.name == "nt" else "/x", ""]
)
def test_paths_cannot_escape_the_run_directory(tmp_path: Path, bad: str) -> None:
    with pytest.raises(StoreError):
        make(tmp_path).path(bad)


def test_spec_round_trip_is_yaml_on_disk(tmp_path: Path) -> None:
    store = make(tmp_path)
    node = store.add_node("n1")
    spec = ResultSpec(id="n1", status="done", summary="ok")
    target = store.write_spec(f"{node.name}/result.spec.yaml", spec)
    assert target.read_text(encoding="utf-8").startswith("spec_version: 1")
    back = store.read_spec(f"{node.name}/result.spec.yaml", ResultSpec)
    assert (back.id, back.status, back.summary) == ("n1", "done", "ok")


def test_update_run_changes_state(tmp_path: Path) -> None:
    store = make(tmp_path)
    store.add_node("n1")

    def finish(record: RunRecord) -> None:
        record.state = "done"
        record.nodes["n1"].state = "done"
        record.final_commit = "abcdef1"

    store.update_run(finish)
    again = store.read_run()
    assert again.state == "done"
    assert again.nodes["n1"].state == "done"
    assert again.final_commit == "abcdef1"


def test_run_json_is_valid_json_with_unknown_fields_preserved(tmp_path: Path) -> None:
    store = make(tmp_path)
    data = json.loads(store.run_json.read_text(encoding="utf-8"))
    data["future"] = {"x": 1}
    atomic_write(store.run_json, json.dumps(data))
    store.update_run(lambda r: setattr(r, "state", "blocked"))
    assert json.loads(store.run_json.read_text(encoding="utf-8"))["future"] == {"x": 1}


# --- atomic writes ----------------------------------------------------------------------------


def test_atomic_write_replaces_content(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    atomic_write(target, "one")
    atomic_write(target, b"two")
    assert target.read_text() == "two"
    assert list(tmp_path.iterdir()) == [target]


def test_failed_replace_keeps_old_content_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.txt"
    atomic_write(target, "old")

    def boom(_src: object, _dst: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(store_module.os, "replace", boom)
    with pytest.raises(OSError, match="disk gone"):
        atomic_write(target, "new")
    assert target.read_text() == "old"
    assert list(tmp_path.iterdir()) == [target]


def test_crash_between_temp_write_and_rename_is_invisible_and_swept(tmp_path: Path) -> None:
    target = tmp_path / "run.json"
    atomic_write(target, '{"v": 1}')
    (tmp_path / ".tmp-run.json.999.dead").write_text(
        '{"v": 2, "half'
    )  # a crashed writer's leftover
    assert json.loads(target.read_text()) == {"v": 1}
    assert sweep_temp_files(tmp_path) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["run.json"]


def test_open_sweeps_leftover_temp_files(tmp_path: Path) -> None:
    store = make(tmp_path)
    (store.run_dir / ".tmp-run.json.1.x").write_text("junk")
    RunStore.open(tmp_path, store.run_id)
    assert not list(store.run_dir.glob(".tmp-*"))


def test_windows_replace_retries_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module.os, "name", "nt")
    real = os.replace
    calls = {"n": 0}

    def flaky(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("held open")
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module.os, "replace", flaky)
    monkeypatch.setattr(store_module, "_fsync_dir", _ignore_path)
    target = tmp_path / "f"
    atomic_write(target, "x")
    assert target.read_text() == "x"
    assert calls["n"] == 3


WRITER = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from relay.core.store import atomic_write

    target = Path(sys.argv[1])
    i = 0
    while True:
        i += 1
        atomic_write(target, json.dumps({"i": i, "pad": "x" * 200_000}))
        if i == 3:
            print("ready", flush=True)
    """
)


def test_killing_a_writer_never_leaves_a_partial_file(tmp_path: Path) -> None:
    """A real process is killed mid-loop; the target always parses and is never truncated."""
    target = tmp_path / "run.json"
    atomic_write(target, json.dumps({"i": 0, "pad": ""}))
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", WRITER, str(target)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        time.sleep(0.15)
    finally:
        child.kill()
        child.wait()
    loaded = json.loads(store_module.read_text(target))
    assert loaded["i"] >= 3
    assert len(loaded["pad"]) == 200_000
    # Windows can keep a killed process's handle alive for a moment, so a sweep may have to retry.
    deadline = time.monotonic() + 5
    while len(list(tmp_path.iterdir())) > 1 and time.monotonic() < deadline:
        sweep_temp_files(tmp_path)
        time.sleep(0.05)
    assert [p.name for p in tmp_path.iterdir()] == ["run.json"]
