"""Atomic persistence for the gateway model cache."""

from __future__ import annotations

import contextlib
import os
import secrets
import time
from pathlib import Path

_TEMP_PREFIX = ".tmp-"
_REPLACE_RETRIES = 8


def _fsync_dir(path: Path) -> None:
    """Make a rename durable on POSIX; Windows cannot open directories, so it is skipped."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes | str) -> None:
    """Write `path` so that readers see the old or the new content, never a partial file.

    The temp file lives in the same directory (same filesystem), is flushed to disk, then
    renamed over the target. A crash leaves at most an ignorable `.tmp-*` file.
    """
    payload = data.encode("utf-8") if isinstance(data, str) else data
    temp = path.with_name(f"{_TEMP_PREFIX}{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        with open(temp, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temp, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def replace_file(source: Path, target: Path) -> None:
    """`os.replace` that retries briefly on Windows sharing violations."""
    # On Windows a reader, antivirus scanner or indexer can briefly hold the target open.
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def read_text(path: Path) -> str:
    """Read a file that another process may be replacing.

    On Windows a reader can briefly get `PermissionError` while a writer's rename is in flight;
    it retries like the writer does, then gives up and raises.
    """
    for attempt in range(_REPLACE_RETRIES):
        try:
            return path.read_bytes().decode("utf-8")  # no newline translation
        except PermissionError:
            if os.name != "nt" or attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(0.01 * (attempt + 1))
    raise AssertionError("unreachable")
