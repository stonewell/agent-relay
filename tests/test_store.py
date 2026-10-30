from __future__ import annotations

import os
from pathlib import Path

import pytest

from relay.core import store as store_module
from relay.core.store import atomic_write


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

    def ignore_fsync(_path: Path) -> None:
        pass

    monkeypatch.setattr(store_module, "_fsync_dir", ignore_fsync)
    target = tmp_path / "f"
    atomic_write(target, "x")
    assert target.read_text() == "x"
    assert calls["n"] == 3
