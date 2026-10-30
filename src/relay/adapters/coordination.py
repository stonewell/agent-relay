"""Shared coordinator-call types and parsing (design 5.6, 7.4).

A coordinator call is one bounded, restricted CLI invocation returning a single validated
:class:`~relay.core.specs.coordination.CoordinatorResponse`. Adapters share this module for
the schema, the error types and the call record; invocation construction and process
supervision stay in each adapter, reusing the worker infrastructure. One call is one step: a
schema-repair follow-up is the controller's decision and consumes another step (section 5.6).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError

from relay.core.platform.proc import ProcessResult
from relay.core.specs import CoordinatorResponse, schema_for

__all__ = [
    "CoordinatorCall",
    "CoordinatorInvalid",
    "CoordinatorUnavailable",
    "coordinator_schema",
    "parse_coordinator_object",
]


class CoordinatorUnavailable(RuntimeError):
    """The coordinator CLI could not be invoked; coordination fails closed (7.4)."""


class CoordinatorInvalid(ValueError):
    """The call produced no valid response; carries the validation error for one repair."""

    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class CoordinatorCall:
    """The validated response plus the identity retained with each call (section 7.4)."""

    response: CoordinatorResponse
    agent: str
    model: str | None
    cli_version: str | None
    profile_digest: str | None
    schema_version: int
    duration_s: float
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


def coordinator_schema() -> dict[str, Any]:
    """The inline `--json-schema` form: no `$schema`, titles, `pattern` or `discriminator`."""
    schema = schema_for(CoordinatorResponse)
    schema.pop("$schema", None)

    def compact(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: compact(value)
                for key, value in cast("dict[str, Any]", node).items()
                if key not in ("title", "discriminator", "pattern")
            }
        if isinstance(node, list):
            return [compact(value) for value in cast("list[Any]", node)]
        return node

    return cast("dict[str, Any]", compact(schema))


def parse_coordinator_object(candidate: object, proc: ProcessResult) -> CoordinatorResponse:
    """Validate a decoded candidate object, or raise :class:`CoordinatorInvalid`."""
    if proc.timed_out:
        raise CoordinatorInvalid("coordinator call timed out", raw=proc.stdout[-2000:])
    if not isinstance(candidate, dict):
        raise CoordinatorInvalid(
            "coordinator returned no JSON object response", raw=proc.stdout[-2000:]
        )
    try:
        return CoordinatorResponse.model_validate(candidate)
    except ValidationError as exc:
        # Drop input echoes: the error text goes back to the model once for repair.
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise CoordinatorInvalid(
            f"response does not match the coordinator schema: {detail}",
            raw=json.dumps(candidate)[:2000],
        ) from exc
