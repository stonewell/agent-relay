"""Shared readers for the inspection commands (design sections 6, 11 and 14).

``relay status``, ``relay result`` and ``relay clean`` read the run directory directly rather
than through a supervisor or IPC (design section 8.6, phase 1). This module keeps that reading
in one place: run-id validation, ``run.json`` loading, the event log and the result spec.

Reading never mutates the store. Unlike :meth:`relay.core.store.RunStore.open` it does not
sweep leftover temp files, so an observer cannot change a run it does not own. Only a torn
trailing event record is tolerated (``read_events``); corruption earlier in the log is a store
failure, reported without a traceback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from relay.core.events import Event, EventLogCorrupt, read_events
from relay.core.runrecord import RunRecord
from relay.core.specs.result import ResultSpec
from relay.core.store import EVENTS, RUN_JSON, read_text, runs_dir

_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{4,6}$")
_RESULT_FILE = "result.spec.yaml"


class InspectError(RuntimeError):
    """A run, node or result cannot be read; the message is actionable and path-safe."""


def check_run_id(run_id: str) -> str:
    """Reject an id that could escape ``.agents/runs`` or forge a git ref prefix."""
    if not _ID_RE.fullmatch(run_id):
        raise InspectError(f"{run_id!r} is not a valid run id (expected <UTC date>-<4 hex digits>)")
    return run_id


def run_directory(repo_root: Path, run_id: str) -> Path:
    return runs_dir(repo_root) / check_run_id(run_id)


@dataclass(frozen=True)
class RunView:
    """One run's durable records, read without taking ownership."""

    run_id: str
    directory: Path
    record: RunRecord
    events: tuple[Event, ...]

    @property
    def events_path(self) -> Path:
        return self.directory / EVENTS


def load_run(repo_root: Path, run_id: str) -> RunView:
    """Load ``run.json`` and the event log of ``run_id`` or raise :class:`InspectError`."""
    directory = run_directory(repo_root, run_id)
    if not directory.is_dir():
        raise InspectError(
            f"run {run_id!r} not found under {runs_dir(repo_root)}; "
            "run `relay status` to list available runs"
        )
    run_json = directory / RUN_JSON
    if not run_json.is_file():
        raise InspectError(
            f"run {run_id!r} has no {RUN_JSON} under {directory}; "
            f"the run directory is incomplete (try `relay clean {run_id} --purge-runs`)"
        )
    try:
        record = RunRecord.model_validate_json(read_text(run_json))
    except (OSError, ValueError) as exc:
        raise InspectError(f"cannot read {run_json}: {exc}") from exc
    if record.run_id != run_id:
        raise InspectError(
            f"{run_json} records run id {record.run_id!r}, not {run_id!r}; "
            "the run directory may have been tampered with"
        )
    return RunView(run_id, directory, record, tuple(_read_events(directory)))


def _read_events(directory: Path) -> list[Event]:
    path = directory / EVENTS
    if not path.is_file():
        return []
    try:
        return read_events(path)
    except (EventLogCorrupt, OSError) as exc:
        raise InspectError(f"cannot read {path}: {exc}") from exc


def last_event(view: RunView, *types: str) -> Event | None:
    """The newest event whose type is one of ``types`` (all types when none are given)."""
    for event in reversed(view.events):
        if not types or event.type in types:
            return event
    return None


def node_ids(view: RunView) -> list[str]:
    """Recorded node ids, falling back to ``NN-<id>`` directories for a sparse record."""
    if view.record.nodes:
        return list(view.record.nodes)
    found: list[str] = []
    try:
        children = sorted(view.directory.iterdir())
    except OSError:
        return []
    for child in children:
        if child.is_dir() and re.match(r"^\d{2}-", child.name):
            found.append(child.name[3:])
    return found


def node_result_path(view: RunView, node_id: str) -> Path | None:
    """The current attempt's ``result.spec.yaml`` for a node, if any.

    The phase 1 writer stores node specs both under the recorded ``NN-<node>`` directory and
    under ``<node>/`` (the run store's ``write_spec`` path); reading accepts either so a
    historical layout never hides a result.
    """
    candidates: list[Path] = []
    node = view.record.nodes.get(node_id)
    if node is not None:
        candidates.append(view.directory / node.dir / _RESULT_FILE)
    candidates.append(view.directory / node_id / _RESULT_FILE)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_result(view: RunView, node_id: str) -> tuple[ResultSpec, Path] | None:
    """Read and validate a node's result spec; ``None`` when the attempt wrote none."""
    path = node_result_path(view, node_id)
    if path is None:
        return None
    try:
        data: Any = yaml.safe_load(read_text(path))
    except (OSError, yaml.YAMLError) as exc:
        raise InspectError(f"cannot read {path}: {exc}") from exc
    try:
        return ResultSpec.model_validate(data), path
    except ValueError as exc:
        raise InspectError(f"invalid result spec {path}: {exc}") from exc


# --- ages --------------------------------------------------------------------------------------


def parse_time(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from the store; ``None`` when it is absent or malformed."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def age_seconds(value: str | None, *, now: datetime | None = None) -> int | None:
    moment = parse_time(value)
    if moment is None:
        return None
    current = datetime.now(UTC) if now is None else now
    return max(0, int((current - moment.astimezone(UTC)).total_seconds()))


def format_age(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def display_path(path: Path, repo_root: Path) -> str:
    """A repository-relative path when possible, so output carries no machine-local prefix."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
