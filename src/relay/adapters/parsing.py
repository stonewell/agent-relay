"""Shared result parsing and validation for adapters (design sections 5.4 and 7.3).

Task 1.11. Owns the rules each adapter used to keep locally:

- the four-step parsing order of section 7.3 (:func:`parse_sources`);
- schema generation for the CLIs' structured-output flags (:func:`claude_schema`,
  :func:`strict_schema`);
- normalisation and validation of an untrusted result (:func:`normalize_result`,
  :func:`validate_result`);
- the single schema-repair follow-up per attempt (:func:`parse_with_repair`, sections 5.4
  and 8.2).

Adapters keep only their native-payload extraction and any CLI-specific enrichment
(session id, model, usage) and call back into this module.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import yaml
from pydantic import ValidationError

from relay.core.specs import ResultSpec, schema_for
from relay.core.store import read_text
from relay.core.yamlio import load_yaml

from .base import RESULT_FILE, RunContext

__all__ = [
    "ABSENT",
    "RepairOutcome",
    "ResultInvalid",
    "claude_schema",
    "drop_nulls",
    "node_file_source",
    "normalize_result",
    "parse_sources",
    "parse_with_repair",
    "stdout_block_source",
    "strict_schema",
    "strip_operational_fields",
    "validate_result",
]

ABSENT: object = object()
"""A parsing source produced nothing (unlike one that produced an empty dict)."""

_BLOCK = re.compile(r"```(?:json|yaml)\s*\n(.*?)\n```", re.DOTALL)

Source = Callable[[], object]
"""One parsing step: returns a candidate object or :data:`ABSENT` to mean "nothing here"."""


class ResultInvalid(ValueError):
    """A result that cannot be parsed or validated against the result spec (section 5.4)."""


# --- schema generation (section 5.4) ---------------------------------------------------------


def claude_schema() -> dict[str, Any]:
    """The result-spec schema for Claude's inline ``--json-schema``, without ``$schema``.

    Claude rejects a schema carrying a ``$schema`` key (section 5.4, verified in
    ``tests/test_recorded_outputs.py``), so the adapter passes this stripped form inline.
    """
    schema = schema_for(ResultSpec)
    schema.pop("$schema", None)
    return schema


def _accepts_null(node: Any) -> bool:
    """True when a JSON Schema node already accepts ``null``."""
    if not isinstance(node, dict):
        return False
    schema = cast("dict[str, Any]", node)
    node_type = schema.get("type")
    if node_type == "null":
        return True
    if isinstance(node_type, list) and "null" in node_type:
        return True
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and any(
            _accepts_null(branch) for branch in cast("list[Any]", branches)
        ):
            return True
    return False


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a generated JSON Schema to the strict form Codex requires (section 5.4).

    Every object lists all of its properties in ``required``, sets
    ``additionalProperties: false``, and makes each optional property nullable. The
    ``$schema`` key is dropped so the inline value matches what the CLI expects.
    """

    def strictify(node: Any) -> Any:
        if isinstance(node, list):
            return [strictify(item) for item in cast("list[Any]", node)]
        if not isinstance(node, dict):
            return node
        original = cast("dict[str, Any]", node)
        result: dict[str, Any] = {key: strictify(value) for key, value in original.items()}
        props = result.get("properties")
        if isinstance(props, dict):
            properties = cast("dict[str, Any]", props)
            required = set(cast("list[str]", original.get("required", ())))
            converted: dict[str, Any] = {}
            for name, prop_schema in properties.items():
                if name not in required and not _accepts_null(prop_schema):
                    prop_schema = {"anyOf": [prop_schema, {"type": "null"}]}
                converted[name] = prop_schema
            result["properties"] = converted
            result["required"] = list(converted)
            result["additionalProperties"] = False
        return result

    converted = strictify(schema)
    converted.pop("$schema", None)
    return converted


def drop_nulls(value: Any) -> Any:
    """Drop ``null`` dict values from a strict-schema reply before validation (section 5.4).

    Only dict values are dropped; ``null`` list items are kept, matching the reference
    behaviour in ``tests/test_recorded_outputs.py``.
    """
    if isinstance(value, dict):
        return {
            key: drop_nulls(item)
            for key, item in cast("dict[str, Any]", value).items()
            if item is not None
        }
    if isinstance(value, list):
        return [drop_nulls(item) for item in cast("list[Any]", value)]
    return value


# --- normalisation and validation -------------------------------------------------------------


def normalize_result(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise an untrusted result before validation (section 5.4).

    - ``null`` values are dropped (Codex strict-schema replies emit them for optional
      fields; Cursor emits ``default: null``).
    - an empty ``options`` list on a ``free`` question is removed (Cursor emits it; the
      ``OpenQuestion`` model rejects any ``options`` on a ``free`` question).
    """
    normalized = drop_nulls(data)
    if not isinstance(normalized, dict):
        raise ResultInvalid(f"result is not an object (got {type(normalized).__name__})")
    result = cast("dict[str, Any]", normalized)
    questions = result.get("open_questions")
    if isinstance(questions, list):
        for question in cast("list[Any]", questions):
            if not isinstance(question, dict):
                continue
            entry = cast("dict[str, Any]", question)
            if entry.get("kind") == "free" and entry.get("options") == []:
                del entry["options"]
    return result


def validate_result(candidate: object, *, node_id: str, agent: str) -> ResultSpec:
    """Normalise and validate a result candidate, raising :class:`ResultInvalid` on failure.

    ``node_id`` and ``agent`` are always forced from the orchestrator/adapter; an
    agent-reported id or agent is never trusted (section 5.4). The other operational fields
    (``model``, ``session``, ``usage``) are left untouched here so the CLI-envelope
    enrichment (and the fake adapter's scripted scenario) can supply them after validation;
    real adapters strip them first via :func:`strip_operational_fields`.
    """
    if not isinstance(candidate, dict):
        raise ResultInvalid(f"result is not an object (got {type(candidate).__name__})")
    data = normalize_result(cast("dict[str, Any]", candidate))
    data["id"] = node_id
    data["agent"] = agent
    try:
        return ResultSpec.model_validate(data)
    except ValidationError as exc:
        raise ResultInvalid(str(exc)) from exc


_OPERATIONAL_FIELDS = ("id", "agent", "model", "session", "usage")


def strip_operational_fields(candidate: object) -> object:
    """Drop agent-supplied operational fields from a candidate before validation.

    ``id``, ``agent``, ``model``, ``session`` and ``usage`` are owned by the orchestrator or
    derived from the CLI envelope, never the agent (sections 5.4, 8.5). A malformed value
    (e.g. ``"usage": "evil"``) must not fail validation and burn the single repair follow-up,
    so these keys are removed before :func:`validate_result` runs. Non-dict candidates pass
    through untouched so :func:`validate_result` can raise its "not an object" error.
    """
    if not isinstance(candidate, dict):
        return candidate
    data = cast("dict[str, Any]", candidate).copy()
    for key in _OPERATIONAL_FIELDS:
        data.pop(key, None)
    return data


# --- the four-step parsing order (section 7.3) -------------------------------------------------


def parse_sources(
    sources: Sequence[Source],
    finalize: Callable[[object], ResultSpec],
) -> ResultSpec:
    """Run the four-step parsing order of section 7.3 and return the first usable result.

    Each source returns a candidate object or :data:`ABSENT` to mean "nothing here"; the
    first non-``ABSENT`` candidate is handed to ``finalize`` (validation plus any
    adapter-specific enrichment). A malformed or unreadable node-dir or block source raises
    :class:`ResultInvalid`; the synthesized source must always produce a candidate.
    """
    for source in sources:
        try:
            candidate = source()
        except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
            raise ResultInvalid(str(exc)) from exc
        if candidate is ABSENT:
            continue
        return finalize(candidate)
    raise AssertionError("unreachable: the synthesized candidate always returns")


def node_file_source(ctx: RunContext) -> object:
    """Parsing step 2: a ``result.spec.yaml`` written by the agent in the node dir."""
    path = ctx.node_dir / RESULT_FILE
    if not path.is_file():
        return ABSENT
    return load_yaml(read_text(path))


def stdout_block_source(stdout: str) -> object:
    """Parsing step 3: the first fenced JSON/YAML block in stdout, or ``ABSENT``."""
    match = _BLOCK.search(stdout)
    if match is None:
        return ABSENT
    return load_yaml(match.group(1))


# --- the single schema-repair follow-up (sections 5.4 and 8.2) --------------------------------

ParseFn = Callable[[], Awaitable[ResultSpec]]
"""Parse and validate one result; raises :class:`ResultInvalid` on failure."""

RepairFn = Callable[[str], Awaitable[ResultSpec]]
"""Run the repair follow-up (same attempt) and re-parse; raises :class:`ResultInvalid`
again on a second failure. Receives the first validation error message."""


@dataclass(frozen=True)
class RepairOutcome:
    """The outcome of :func:`parse_with_repair`.

    ``result`` is the validated result, or ``None`` when the attempt ended in a terminal
    (second) validation failure. ``attempt`` is the attempt the repair was charged to,
    passed through unchanged. ``repaired`` records that the one repair follow-up ran;
    ``error`` carries the second validation error when the attempt failed.
    """

    result: ResultSpec | None
    attempt: int
    repaired: bool
    error: str | None = None

    @property
    def failed(self) -> bool:
        """True when the attempt ended in a terminal failure (the node must be marked failed)."""
        return self.result is None


async def parse_with_repair(
    parse: ParseFn,
    repair: RepairFn,
    *,
    attempt: int,
) -> RepairOutcome:
    """Parse, and on one validation failure run exactly one repair follow-up (5.4 / 8.2).

    ``parse`` returns a validated result or raises :class:`ResultInvalid`. On the first
    failure, ``repair(error_message)`` is called exactly once: it performs the repair
    follow-up in the *same* attempt and returns a validated result, or raises
    :class:`ResultInvalid` for the second failure. A second failure is terminal
    (``result is None``) and never triggers another repair. The attempt number is passed
    through unchanged, so the repair is charged to the same attempt and a second failure
    marks the node ``failed`` rather than consuming a second attempt or another repair.
    """
    try:
        return RepairOutcome(result=await parse(), attempt=attempt, repaired=False)
    except ResultInvalid as first:
        try:
            result = await repair(str(first))
        except ResultInvalid as second:
            return RepairOutcome(result=None, attempt=attempt, repaired=True, error=str(second))
        return RepairOutcome(result=result, attempt=attempt, repaired=True)
