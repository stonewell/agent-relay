"""Run store: directory layout, atomic writes, `run.json`, run ids (design section 6)."""

from __future__ import annotations

import contextlib
import os
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from .runrecord import NodeRecord, RunRecord
from .specs import parse_spec
from .specs.common import NodeId, RunId, SpecModel, normalize_repo_path

AGENTS_DIR = ".agents"
RUNS_DIR = "runs"
LOCKS_DIR = "locks"
RUN_JSON = "run.json"
EVENTS = "events.jsonl"
ROUTES_JSONL = "routes.jsonl"
ROUTE_LOG = "route.log"
PROJECT_SPEC = "PROJECT.spec.md"

_TEMP_PREFIX = ".tmp-"
_REPLACE_RETRIES = 8

M = TypeVar("M", bound=BaseModel)


class StoreError(RuntimeError):
    """The run store cannot complete an operation."""


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


def sweep_temp_files(directory: Path) -> int:
    """Remove temp files left behind by a crashed writer; returns how many were removed."""
    removed = 0
    for leftover in directory.glob(f"{_TEMP_PREFIX}*"):
        with contextlib.suppress(OSError):
            leftover.unlink()
            removed += 1
    return removed


def new_run_id(now: datetime, *, hex_digits: int = 4) -> RunId:
    return f"{now.astimezone(UTC):%Y-%m-%d}-{secrets.token_hex(hex_digits // 2)}"


def _now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunStore:
    """One run directory under `<repo>/.agents/runs/`. The orchestrator is its single writer."""

    def __init__(self, repo_root: Path, run_id: RunId) -> None:
        self._lock = threading.RLock()
        self.repo_root = repo_root
        self.run_id = run_id
        self.run_dir = runs_dir(repo_root) / run_id

    # --- creation and discovery -----------------------------------------------------------

    @classmethod
    def create(
        cls,
        repo_root: Path,
        *,
        config_hash: str | None = None,
        now: Callable[[], datetime] = _now,
        run_id: RunId | None = None,
    ) -> RunStore:
        """Create a new run directory and its initial `run.json`.

        The directory is made with an exclusive `mkdir`, so concurrent creators cannot share an
        id. A collision, or an id whose lock object still exists, draws a new suffix.
        """
        parent = runs_dir(repo_root)
        parent.mkdir(parents=True, exist_ok=True)
        moment = now()
        for attempt in range(40):
            candidate = run_id or new_run_id(moment, hex_digits=4 if attempt < 10 else 6)
            if _lock_exists(repo_root, candidate):
                if run_id:
                    raise StoreError(f"run id {candidate} was used before and cannot be reused")
                continue
            try:
                (parent / candidate).mkdir()
            except FileExistsError:
                if run_id:
                    raise StoreError(f"run {candidate} already exists") from None
                continue
            store = cls(repo_root, candidate)
            store.write_run(
                RunRecord(run_id=candidate, created_at=iso(moment), config_hash=config_hash)
            )
            return store
        raise StoreError("could not allocate a unique run id")

    @classmethod
    def open(cls, repo_root: Path, run_id: RunId) -> RunStore:
        store = cls(repo_root, run_id)
        if not store.run_dir.is_dir():
            raise StoreError(f"run {run_id} not found under {runs_dir(repo_root)}")
        sweep_temp_files(store.run_dir)
        return store

    @staticmethod
    def list_runs(repo_root: Path) -> list[RunId]:
        parent = runs_dir(repo_root)
        if not parent.is_dir():
            return []
        return sorted(p.name for p in parent.iterdir() if (p / RUN_JSON).is_file())

    # --- paths ----------------------------------------------------------------------------

    def path(self, relative: str) -> Path:
        """Resolve a path inside the run directory; anything that escapes it is refused."""
        try:
            safe = normalize_repo_path(relative)
        except ValueError as exc:
            raise StoreError(f"{relative!r} is not a path inside the run directory") from exc
        if ":" in safe:  # NTFS alternate data streams and drive-relative forms
            raise StoreError(f"{relative!r} is not a path inside the run directory")
        return self.run_dir.joinpath(*PurePosixPath(safe).parts)

    @property
    def run_json(self) -> Path:
        return self.run_dir / RUN_JSON

    @property
    def events_path(self) -> Path:
        return self.run_dir / EVENTS

    @property
    def routes_path(self) -> Path:
        return self.run_dir / ROUTES_JSONL

    @property
    def route_log_path(self) -> Path:
        return self.run_dir / ROUTE_LOG

    # --- run.json -------------------------------------------------------------------------

    def read_run(self) -> RunRecord:
        return RunRecord.model_validate_json(read_text(self.run_json))

    def write_run(self, record: RunRecord) -> None:
        atomic_write(self.run_json, record.model_dump_json(indent=2, exclude_none=True) + "\n")

    def update_run(self, change: Callable[[RunRecord], None]) -> RunRecord:
        """Read, modify and atomically rewrite `run.json` (single writer, no locking here)."""
        with self._lock:
            record = self.read_run()
            change(record)
            self.write_run(record)
            return record

    # --- nodes ----------------------------------------------------------------------------

    def add_node(self, node_id: NodeId) -> Path:
        """Create the next `NN-<node-id>/` directory and record it in `run.json`."""
        with self._lock:
            record = self.read_run()
            if node_id in record.nodes:
                return self.run_dir / record.nodes[node_id].dir
            name = f"{len(record.nodes) + 1:02d}-{node_id}"
            directory = self.run_dir / name
            directory.mkdir()
            record.nodes[node_id] = NodeRecord(dir=name)
            self.write_run(record)
            return directory

    def node_dir(self, node_id: NodeId) -> Path:
        record = self.read_run()
        if node_id not in record.nodes:
            raise StoreError(f"node {node_id} is not part of run {self.run_id}")
        return self.run_dir / record.nodes[node_id].dir

    # --- specs and files ------------------------------------------------------------------

    def write_spec(self, relative: str, spec: SpecModel) -> Path:
        """Write a spec as YAML atomically (design section 5)."""
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        data: Any = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
        atomic_write(target, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        return target

    def read_spec(self, relative: str, model: type[M]) -> M:
        return parse_spec(model, read_text(self.path(relative)))

    def write_text(self, relative: str, text: str) -> Path:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, text)
        return target


def runs_dir(repo_root: Path) -> Path:
    return repo_root / AGENTS_DIR / RUNS_DIR


def locks_dir(repo_root: Path) -> Path:
    return repo_root / AGENTS_DIR / LOCKS_DIR


def _lock_exists(repo_root: Path, run_id: RunId) -> bool:
    directory = locks_dir(repo_root)
    return directory.is_dir() and any(directory.glob(f"*-{run_id}.lock"))
