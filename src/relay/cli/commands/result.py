"""``relay result`` (design sections 5.3, 6 and 14; task 1.22).

Reads the run record and the node's current ``result.spec.yaml`` directly. A missing result file
is not an error: a failed or ``needs_input`` attempt may leave only the record, and the command
still reports what is known. Every field of the result is optional in the payload.

``relay result <run> <node> [field]`` is the design's full form. Phase 1 has one node, so
``<node>`` may be omitted when the run records exactly one; ``[field]`` prints one dotted field
(for example ``changes.files``) instead of the whole result.

A missing run or unknown node is invalid input (exit 2); an unreadable run or result is a store
failure (exit 1). No prompt, log or environment value is read.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from relay.core.publish import integration_branch
from relay.core.specs.result import ResultSpec

from ..exitcodes import Exit
from .inspect_common import (
    InspectError,
    RunView,
    display_path,
    load_result,
    load_run,
    node_ids,
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("result", help="show a node's result spec")
    parser.add_argument("run_id", metavar="RUN", help="the run to read")
    parser.add_argument(
        "node", nargs="?", metavar="NODE", help="the node (default: the run's only node)"
    )
    parser.add_argument(
        "field",
        nargs="?",
        metavar="FIELD",
        help="print one dotted field, for example changes.files",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def run(args: argparse.Namespace, repo_root: Path) -> int:
    as_json = bool(args.json)
    run_id: str = args.run_id
    try:
        view = load_run(repo_root, run_id)
    except InspectError as exc:
        return _fail(f"relay result: {exc}", _exit_for(exc), as_json)

    try:
        node_id = _select_node(view, args.node)
    except InspectError as exc:
        return _fail(f"relay result: {exc}", Exit.INVALID, as_json)

    try:
        loaded = load_result(view, node_id)
    except InspectError as exc:
        return _fail(f"relay result: {exc}", Exit.FAILURE, as_json)

    result, path = loaded if loaded is not None else (None, None)
    payload = _payload(view, node_id, result, path, repo_root)

    if args.field is not None:
        try:
            value = _field(payload, args.field)
        except InspectError as exc:
            return _fail(f"relay result: {exc}", Exit.INVALID, as_json)
        if as_json:
            _print_json(value)
        else:
            print(_render_field(value))
        return int(Exit.OK)

    if as_json:
        _print_json(payload)
    else:
        print(_render_text(payload, result))
    return int(Exit.OK)


# --- selection --------------------------------------------------------------------------------


def _select_node(view: RunView, requested: str | None) -> str:
    ids = node_ids(view)
    if requested is not None:
        if requested not in ids and not (view.directory / requested).is_dir():
            known = ", ".join(ids) if ids else "none"
            raise InspectError(
                f"node {requested!r} is not part of run {view.run_id!r} (known nodes: {known})"
            )
        return requested
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise InspectError(f"run {view.run_id!r} records no nodes; specify NODE explicitly")
    known = ", ".join(ids)
    raise InspectError(f"run {view.run_id!r} has several nodes ({known}); specify NODE explicitly")


# --- payload ----------------------------------------------------------------------------------


def _payload(
    view: RunView,
    node_id: str,
    result: ResultSpec | None,
    path: Path | None,
    repo_root: Path,
) -> dict[str, Any]:
    record = view.record
    node = record.nodes.get(node_id)
    status = (
        result.status if result is not None else (node.state if node is not None else "unknown")
    )
    changes = result.changes if result is not None else None
    verification = result.verification if result is not None else None
    output_commit = (
        changes.output_commit
        if changes is not None and changes.output_commit is not None
        else (node.output_commit if node is not None else None)
    )
    return {
        "run_id": view.run_id,
        "node_id": node_id,
        "run_state": record.state,
        "node_state": node.state if node is not None else "unknown",
        "status": status,
        "result_found": result is not None,
        "result_path": display_path(path, repo_root) if path is not None else None,
        "summary": result.summary if result is not None else "",
        "agent": result.agent if result is not None else None,
        "model": result.model if result is not None else None,
        "output_commit": output_commit,
        "integration_commit": record.final_commit,
        "integration_branch": integration_branch(view.run_id),
        "changes": (
            {
                "branch": changes.branch,
                "input_commit": changes.input_commit,
                "output_commit": changes.output_commit,
                "commits": list(changes.commits),
                "files": list(changes.files),
            }
            if changes is not None
            else None
        ),
        "verification": (
            {
                "mode": verification.mode,
                "acceptance": verification.acceptance,
                "skip_reason": verification.skip_reason,
                "details": [
                    {"run": detail.run, "exit": detail.exit} for detail in verification.details
                ],
            }
            if verification is not None
            else None
        ),
        "open_questions": (
            [
                question.model_dump(mode="json", exclude_none=True)
                for question in result.open_questions
            ]
            if result is not None
            else []
        ),
        "artifacts": (
            [artifact.model_dump(mode="json", exclude_none=True) for artifact in result.artifacts]
            if result is not None
            else []
        ),
    }


def _field(payload: Mapping[str, Any], field: str) -> Any:
    current: Any = payload
    for part in field.split("."):
        if not part:
            raise InspectError(f"field {field!r} is empty")
        if not isinstance(current, Mapping):
            raise InspectError(f"unknown field {field!r} (try --json to see the available fields)")
        mapping = cast("Mapping[str, Any]", current)
        if part not in mapping:
            raise InspectError(f"unknown field {field!r} (try --json to see the available fields)")
        current = mapping[part]
    return current


# --- output -----------------------------------------------------------------------------------


def _verification_text(verification: Mapping[str, Any] | None) -> str:
    if verification is None:
        return "-"
    outcome = verification.get("acceptance")
    mode = verification.get("mode")
    reason = verification.get("skip_reason")
    text = f"{outcome} ({mode})"
    if isinstance(reason, str) and reason:
        text = f"{text}; skipped: {reason}"
    return text


def _render_text(payload: Mapping[str, Any], result: ResultSpec | None) -> str:
    lines = [f"run {payload['run_id']} node {payload['node_id']}: {payload['status']}"]
    if result is None:
        lines.append("  result spec: not written (the attempt did not finish successfully)")
    lines.extend(
        [
            f"  run state: {payload['run_state']}",
            f"  agent/model: {payload['agent'] or '-'}/{payload['model'] or '-'}",
            f"  summary: {payload['summary'] or '-'}",
            f"  output commit: {payload['output_commit'] or '-'}",
            f"  integration commit: {payload['integration_commit'] or '-'}",
            f"  integration branch: {payload['integration_branch']}",
        ]
    )
    changes = payload.get("changes")
    if isinstance(changes, Mapping):
        change_map = cast("Mapping[str, Any]", changes)
        branch = change_map.get("branch")
        lines.append(f"  changes branch: {branch or '-'}")
        files = change_map.get("files")
        file_list = cast("list[Any]", files) if isinstance(files, list) else []
        count = len(file_list)
        lines.append(f"  changed files: {count}")
        for item in file_list[:20]:
            lines.append(f"    {item}")
        if count > 20:
            lines.append(f"    ... and {count - 20} more")
    else:
        lines.append("  changes: -")
    lines.append(f"  verification: {_verification_text(payload.get('verification'))}")
    questions = payload.get("open_questions")
    if isinstance(questions, list) and questions:
        question_list = cast("list[Any]", questions)
        lines.append(f"  open questions: {len(question_list)}")
        for question in question_list:
            if not isinstance(question, Mapping):
                continue
            question_map = cast("Mapping[str, Any]", question)
            options = question_map.get("options")
            suffix = f" options={options}" if options else ""
            default = question_map.get("default")
            hint = f" default={default}" if default is not None else ""
            lines.append(f"    [{question_map.get('id')}] {question_map.get('text')}{suffix}{hint}")
    else:
        lines.append("  open questions: none")
    return "\n".join(lines)


def _render_field(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)


# --- errors -----------------------------------------------------------------------------------


def _exit_for(exc: InspectError) -> Exit:
    text = str(exc)
    if "not found" in text or "no run.json" in text or "not a valid run id" in text:
        return Exit.INVALID
    return Exit.FAILURE


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _fail(message: str, code: Exit, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(message, file=sys.stderr)
    return int(code)
