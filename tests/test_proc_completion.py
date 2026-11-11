"""Shared process guarantees required by bounded gateway completion requests."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from relay.core.platform import proc
from relay.core.platform.proc import (
    OutputLimitExceeded,
    ProcessDrainError,
    _stream_pump,  # pyright: ignore[reportPrivateUsage]
    run_process,
    stream_output,
)


def test_unterminated_output_is_bounded_before_eof(tmp_path: Path) -> None:
    started = time.monotonic()
    script = "import sys,time;sys.stdout.write('x'*100000);sys.stdout.flush();time.sleep(10)"
    with stream_output(lambda _: None, max_bytes=1024), pytest.raises(OutputLimitExceeded):
        run_process([sys.executable, "-c", script], cwd=tmp_path, env=os.environ, timeout_s=8)
    # The previous unbounded readline waited for process timeout/EOF instead of the cap.
    assert time.monotonic() - started < 5


def test_long_utf8_lines_forward_once_and_capture_exactly(tmp_path: Path) -> None:
    lines: list[str] = []
    script = "import sys;sys.stdout.write('é'*100000+'\\n'+'tail')"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with stream_output(lines.append, max_bytes=400000):
        result = run_process([sys.executable, "-c", script], cwd=tmp_path, env=env, timeout_s=8)
    assert lines == ["é" * 100000 + "\n", "tail"]
    assert result.stdout == "".join(lines)


def test_undrained_pipe_thread_is_error_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate an inherited pipe surviving the kill grace after real child execution.
    def alive(_: threading.Thread) -> bool:
        return True

    monkeypatch.setattr(threading.Thread, "is_alive", alive)
    with stream_output(lambda _: None), pytest.raises(ProcessDrainError):
        run_process(
            [sys.executable, "-c", "print('done')"], cwd=tmp_path, env=os.environ, timeout_s=8
        )


def test_partial_line_is_forwarded_when_capture_overflows(tmp_path: Path) -> None:
    # 65,536 characters are read as an unterminated part; the next part overflows the cap.
    lines: list[str] = []
    script = "import sys,time;sys.stdout.write('a'*200000);sys.stdout.flush();time.sleep(10)"
    with stream_output(lines.append, max_bytes=100000), pytest.raises(OutputLimitExceeded):
        run_process([sys.executable, "-c", script], cwd=tmp_path, env=os.environ, timeout_s=8)
    assert lines == ["a" * 65536]


def test_partial_line_is_forwarded_when_the_pipe_fails() -> None:
    class Broken:
        def __init__(self) -> None:
            self.parts = ["partial"]

        def readline(self, _size: int) -> str:
            if self.parts:
                return self.parts.pop()
            raise OSError("pipe closed")

    lines: list[str] = []
    chunks: list[str] = []
    _stream_pump(
        Broken(),  # pyright: ignore[reportArgumentType]
        chunks,
        [0],
        1024,
        threading.Event(),
        lambda: None,
        lines.append,
    )
    assert chunks == lines == ["partial"]


def test_closed_job_handle_is_never_terminated_again(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, int | None]] = []

    def terminate(handle: int) -> None:
        events.append(("terminate", handle))

    def close(handle: int | None) -> None:
        events.append(("close", handle))

    monkeypatch.setattr(proc, "_terminate_job", terminate)
    monkeypatch.setattr(proc, "_close_job", close)
    job = proc._JobHandle(42)  # pyright: ignore[reportPrivateUsage]
    job.terminate()  # leader may have exited; descendants still need the job kill
    job.close()
    job.terminate()  # a late pump-thread kill after close must not touch a reused handle
    job.close()
    assert events == [("terminate", 42), ("close", 42), ("close", None)]
