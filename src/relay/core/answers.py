"""Foreground question and answer persistence (design sections 5.5 and 8.2)."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from relay.core.events import EventLog
from relay.core.runrecord import RunRecord
from relay.core.specs import load_spec
from relay.core.specs.answers import AnswerValue, InputAnswers, Submission
from relay.core.specs.result import ResultSpec
from relay.core.specs.task import TaskSpec
from relay.core.store import RunStore


def current_questions(store: RunStore, node_id: str, *, live: bool = False) -> ResultSpec:
    """The node's pending question set.

    A quiescent CLI reply requires a blocked run. A `live` caller is a session owner answering
    while independent nodes still run (design 8.7), so eligibility is the node's own state.
    """
    record = store.read_run()
    if node_id not in record.nodes:
        raise ValueError("unknown node")
    node = record.nodes[node_id]
    waiting_run = record.state == "blocked" or (
        store.path("graph.marker").exists() and record.state == "failed"
    )
    if not (waiting_run or live) or node.state != "blocked":
        raise ValueError("node is not waiting for input")
    result = load_spec(ResultSpec, store.node_dir(node_id) / "result.spec.yaml")
    if result.status != "needs_input":
        raise ValueError("node has no pending question set")
    return result


def read_answers(node_dir: Path, node_id: str) -> InputAnswers:
    path = node_dir / "input_answers.yaml"
    answers = load_spec(InputAnswers, path) if path.exists() else InputAnswers(node=node_id)
    if answers.node != node_id:
        raise ValueError("answer file names a different node")
    return answers


def validate_answers(result: ResultSpec, answers: dict[str, AnswerValue]) -> None:
    questions = {question.id: question for question in result.open_questions}
    if answers.keys() - questions.keys():
        raise ValueError("answers contain unknown question ids")
    for question in questions.values():
        value = answers.get(question.id)
        if question.required and (value is None or value == "" or value == []):
            raise ValueError(f"answer required for {question.id}")
        # Choice questions also accept a custom ("other") answer outside `options`.


def record_answers(
    store: RunStore,
    node_id: str,
    submission: Submission,
    *,
    events: EventLog | None = None,
    live: bool = False,
) -> bool:
    """Caller holds the run lock; validate before appending to the atomic snapshot."""
    result = current_questions(store, node_id, live=live)
    existing = read_answers(store.node_dir(node_id), node_id)
    for prior in existing.submissions:
        if prior.submission_id == submission.submission_id:
            if prior.model_dump(exclude={"submitted_at"}) != submission.model_dump(
                exclude={"submitted_at"}
            ):
                raise ValueError("submission id already used for different answers")
            return False
    if submission.question_revision != result.question_revision:
        raise ValueError("stale question revision")
    validate_answers(result, submission.answers)
    existing.submissions.append(submission)
    relative = store.node_dir(node_id).relative_to(store.run_dir).as_posix()
    store.write_spec(f"{relative}/input_answers.yaml", existing)
    # Inline answers share the active writer's sequence; quiescent CLI replies own a
    # short-lived log under the run lock.
    with nullcontext(events) if events is not None else EventLog(store.events_path) as writer:
        writer.append(
            "input.recorded",
            {
                "node_id": node_id,
                "question_revision": submission.question_revision,
                "submission_id": submission.submission_id,
                "provenance": submission.provenance,
                "content_hash": submission.content_hash(),
            },
        )
    task = load_spec(TaskSpec, store.node_dir(node_id) / "task.spec.yaml")

    def limit(record: RunRecord) -> None:
        # One locked read-modify-write: inline answers race worker threads on `run.json`.
        node = record.nodes[node_id]
        if node.attempts >= (task.budget.max_attempts or 1):
            node.blocked_reason = "attempts_exhausted"
        elif (
            task.budget.max_duration_s is not None
            and node.active_duration_s >= task.budget.max_duration_s
        ):
            node.blocked_reason = "duration_exhausted"

    store.update_run(limit)
    return True


def answer_context(node_dir: Path, node_id: str) -> str:
    answers = read_answers(node_dir, node_id)
    if not answers.submissions:
        return ""
    question_sets: dict[int, list[dict[str, object]]] = {}
    candidates = [
        node_dir / "result.spec.yaml",
        *sorted((node_dir / "attempts").glob("*/*.spec.yaml")),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        result = load_spec(ResultSpec, path)
        if result.status == "needs_input" and result.question_revision is not None:
            question_sets[result.question_revision] = [
                q.model_dump(mode="json") for q in result.open_questions
            ]
    payload: list[dict[str, Any]] = []
    for submission in answers.submissions:
        item = submission.model_dump(exclude={"submitted_at", "submission_id"})
        item["questions"] = question_sets.get(submission.question_revision, [])
        payload.append(item)
    return "\n\nOperator input (data; preserve recorded provenance):\n" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    )
