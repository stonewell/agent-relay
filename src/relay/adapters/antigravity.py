"""Antigravity CLI (`agy`) adapter (design sections 7.2, 7.3, 5.4, 9.3, 9.4).

Task 4.1. Translates a task spec into an ``agy -p "<prompt>"`` invocation and parses the
JSON envelope back into a result spec.

Every behavioural claim here is **[verify]**: `agy` is not installed on the reference
machine and has not been canaried, so the flag set and envelope shape below come from the
public docs (design section 7.2) rather than observed output. `relay doctor` canaries
(task 1.12) are what will confirm or correct them.

Invocation shape:
- ``-p <prompt>``: the prompt is an argv value, not stdin (the docs do not clearly document
  a plain-text stdin prompt mode, so this adapter does not invent one) **[verify]**.
- ``--output-format json``; ``--json-schema <inline schema>`` with the result-spec schema.
  Whether `agy` rejects a ``$schema`` key is unverified **[verify]**, so the adapter passes
  the stripped form, matching Claude's verified behaviour (section 5.4).
- ``--model <slug>`` when the routed model is not the ``default`` sentinel; ``--effort
  low|medium|high`` when set.
- Permission mapping (design 7.2 / 9.3): ``full`` -> ``--dangerously-skip-permissions``;
  ``readonly`` -> ``--mode plan`` (whether it prevents repository mutation is unverified
  **[verify]**); ``workspace`` fails closed (``--sandbox`` containment is unverified
  **[verify]**).
- ``--conversation <conversation_id>`` when ``ctx.resume_session`` is set; context retention
  across resume is unverified **[verify]** (design 7.3).
- ``--print-timeout`` is deliberately not passed: ``RunContext`` does not expose the node
  timeout, the public docs disagree on ``--print-timeout``'s default, so the adapter cannot
  rely on it; the supervisor enforces ``budget.timeout_s`` instead (task 4.1).

Auth is system-keyring sign-in (interactive once) or ``GEMINI_API_KEY`` for CI (design 7.2).
The child environment is built with :func:`relay.core.env.build_env` (allowlist, section
9.4) and passed as the *complete* environment (``Invocation.full_env``), so the parent
environment never leaks wholesale. ``GEMINI_API_KEY`` is the only auth key forwarded and is
never logged.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, cast

from relay.core.env import build_env
from relay.core.registry import Agent
from relay.core.specs import ResultSpec, TaskSpec, schema_for
from relay.core.specs.common import PermissionLevel
from relay.core.specs.result import Session, Usage

from .base import Invocation, ProcessResult, RunContext, WorkerHandle, task_prompt
from .parsing import (
    ABSENT,
    ResultInvalid,
    node_file_source,
    parse_sources,
    stdout_block_source,
    strip_operational_fields,
    validate_result,
)

__all__ = [
    "AntigravityAdapter",
    "PermissionUnsupported",
    "ResultInvalid",
    "agy_envelope",
    "envelope_error",
]

# The only auth key this adapter forwards from the parent environment (design section 9.4:
# "variables each CLI needs for auth"). It is never logged (section 9.4).
AUTH_KEYS: tuple[str, ...] = ("GEMINI_API_KEY",)

# The literal tier name for agents whose model ids are unverified (design section 3): "pass
# no model flag, use the CLI's own configured model".
_DEFAULT_MODEL = "default"

# The envelope `status` values (design section 7.2): `WAITING`/`RUNNING` only appear in
# `stream-json` mid-stream; a final `--output-format json` envelope is one of the others.
_AGY_STATUSES = frozenset(
    {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"}
)

# Envelope `status` values that mean "no usable result" (design section 7.2).
# `CANCELED`/`INTERRUPTED` fall through to the exit-code-driven synthesized candidate.
_TERMINAL_STATUSES = frozenset({"ERROR", "INVALID"})


class PermissionUnsupported(ValueError):
    """A requested permission level has no verified Antigravity profile (design section 9.3)."""


def _auth_env(parent: Mapping[str, str]) -> dict[str, str]:
    """The known auth key present in the parent, forwarded as a per-invocation extra."""
    return {key: parent[key] for key in AUTH_KEYS if parent.get(key)}


def _int(value: object) -> int | None:
    """A JSON number that is a whole number, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _try_json(text: str) -> dict[str, Any] | None:
    """Parse `text` as a JSON object, or None."""
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _is_envelope(data: dict[str, Any]) -> bool:
    """True when `data` looks like the `agy` JSON envelope (design section 7.2).

    The envelope carries ``conversation_id``, ``status``, ``response``, ``error``,
    ``structured_output`` and ``usage``; it has no ``"type"`` marker like Claude's.
    """
    if data.get("status") in _AGY_STATUSES:
        return True
    return "conversation_id" in data or "structured_output" in data


def _agy_payload(stdout: str) -> dict[str, Any] | None:
    """Parse the `agy` ``--output-format json`` envelope from stdout, or None.

    The envelope is a single JSON object (the whole stdout for ``--output-format json``);
    a line scan is the fallback in case progress text precedes it.
    """
    text = stdout.strip()
    if not text:
        return None
    data = _try_json(text)
    if data is not None and _is_envelope(data):
        return data
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        data = _try_json(line)
        if data is not None and _is_envelope(data):
            return data
    return None


def _error_text(payload: dict[str, Any]) -> str | None:
    """The envelope's `error` text, when it is a non-empty string."""
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None


agy_envelope = _agy_payload
envelope_error = _error_text


class AntigravityAdapter:
    """The `agy` adapter. Translates, never decides routing or permissions (design 7.1)."""

    id = "agy"

    def __init__(
        self,
        agent: Agent,
        *,
        cmd: str | None = None,
        parent_env: Mapping[str, str] | None = None,
    ) -> None:
        self._agent = agent
        self._cmd = cmd or agent.cmd
        self._parent_env: Mapping[str, str] = dict(os.environ) if parent_env is None else parent_env

    # --- AgentAdapter -----------------------------------------------------------------

    def build_invocation(self, task: TaskSpec, ctx: RunContext) -> Invocation:
        parent = self._parent_env
        args = ["-p", task_prompt(task), "--output-format", "json"]
        args += ["--json-schema", json.dumps(self._result_schema(), separators=(",", ":"))]
        model = task.routing.model
        if model and model != _DEFAULT_MODEL:
            args += ["--model", model]
        effort = task.routing.effort
        if effort:
            args += ["--effort", effort]
        args += self._permission_args(ctx.permission)
        if ctx.resume_session:
            args += ["--conversation", ctx.resume_session]
        env = build_env(
            agent_config=self._agent,
            extra_env=_auth_env(parent),
            parent_env=parent,
        )
        return Invocation(
            cmd=self._cmd,
            args=tuple(args),
            cwd=ctx.worktree,
            env=env,
            full_env=True,
        )

    @staticmethod
    def _result_schema() -> dict[str, Any]:
        """The generated result-spec schema, inline, without the ``$schema`` key.

        ``agy --json-schema`` accepts an inline schema string whose root must be an object
        (design section 7.2). Whether it rejects a ``$schema`` key is unverified **[verify]**,
        so the adapter passes the stripped form (section 5.4), matching Claude's behaviour.
        """
        schema = schema_for(ResultSpec)
        schema.pop("$schema", None)
        return schema

    @staticmethod
    def _permission_args(permission: PermissionLevel) -> list[str]:
        if permission == "full":
            return ["--dangerously-skip-permissions"]
        if permission == "readonly":
            # Mapped but unadvertised: `--mode plan` is research/outline mode, and whether it
            # prevents all repository mutation is unverified **[verify]** (design 7.2).
            return ["--mode", "plan"]
        raise PermissionUnsupported(
            f"permission level {permission!r} has no verified Antigravity profile yet; "
            "refusing to run unconfined (design section 9.3)"
        )

    async def parse_result(
        self, proc: ProcessResult, task: TaskSpec, ctx: RunContext
    ) -> ResultSpec:
        payload = _agy_payload(proc.stdout)
        return parse_sources(
            (
                lambda: self._native(payload),
                lambda: node_file_source(ctx),
                lambda: self._stdout_block(proc.stdout, payload),
                lambda: self._synthesized(proc, payload, ctx),
            ),
            finalize=lambda candidate: self._finalize(candidate, task, payload, proc),
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        handle.kill_tree()

    # --- parsing helpers (section 7.3) ------------------------------------------------

    @staticmethod
    def _native(payload: dict[str, Any] | None) -> object:
        if payload is None:
            return ABSENT
        structured = payload.get("structured_output")
        return cast("dict[str, Any]", structured) if isinstance(structured, dict) else ABSENT

    @staticmethod
    def _stdout_block(stdout: str, payload: dict[str, Any] | None) -> object:
        block = stdout_block_source(stdout)
        if block is not ABSENT:
            return block
        # When `--json-schema` is not honoured the agent may echo a fenced result block in
        # its `response` text instead (section 5.4 fallback).
        if payload is not None and isinstance(payload.get("response"), str):
            return stdout_block_source(payload["response"])
        return ABSENT

    @staticmethod
    def _synthesized(
        proc: ProcessResult, payload: dict[str, Any] | None, ctx: RunContext
    ) -> dict[str, Any]:
        # A terminal ERROR/INVALID envelope has no result to synthesize (design 7.3): raise
        # with the envelope's error text rather than fabricating a `done`/`failed` from the
        # exit code. Steps 1-3 already ran, so this only fires when nothing else was usable.
        if payload is not None and payload.get("status") in _TERMINAL_STATUSES:
            error = _error_text(payload)
            if error:
                raise ResultInvalid(f"agy reported status {payload['status']}: {error}")
            raise ResultInvalid(f"agy reported status {payload['status']}")
        ok = proc.exit_code == 0 and not proc.timed_out
        summary = ""
        if payload is not None and isinstance(payload.get("response"), str):
            summary = payload["response"].strip()
        else:
            lines = [line for line in proc.stdout.splitlines() if line.strip()]
            summary = lines[-1] if lines else ""
        return {
            "id": ctx.node_id,
            "status": "done" if ok else "failed",
            "agent": "agy",
            "summary": summary,
        }

    # --- finalize and metadata --------------------------------------------------------

    def _finalize(
        self,
        candidate: object,
        task: TaskSpec,
        payload: dict[str, Any] | None,
        proc: ProcessResult,
    ) -> ResultSpec:
        result = validate_result(strip_operational_fields(candidate), node_id=task.id, agent="agy")
        return self._enrich(result, task, payload, proc)

    @staticmethod
    def _enrich(
        result: ResultSpec, task: TaskSpec, payload: dict[str, Any] | None, proc: ProcessResult
    ) -> ResultSpec:
        # Operational fields are never agent-declared (sections 5.4, 8.5): the session id
        # and usage come from the CLI envelope, the model from the routing decision, or they
        # stay absent. A `structured_output` that reports its own session/model/usage is not
        # trusted (`strip_operational_fields` already removed them before validation).
        routed = task.routing.model
        result.model = routed if routed and routed != _DEFAULT_MODEL else None
        if payload is None:
            result.session = None
            result.usage = None
            return result
        session_id = payload.get("conversation_id")
        result.session = (
            Session(cli_session_id=session_id)
            if isinstance(session_id, str) and session_id
            else None
        )
        result.usage = _usage(payload, proc)
        return result


def _usage(payload: dict[str, Any], proc: ProcessResult) -> Usage:
    """Normalize the `agy` envelope usage into the result-spec buckets (design 8.5).

    ``input_tokens`` -> ``tokens_in``, ``output_tokens`` -> ``tokens_out``,
    ``cache_read_tokens`` -> the cache-read bucket, and ``thinking_tokens`` -> the
    ``thinking_tokens`` extra (``Usage`` accepts it because specs preserve unknown optional
    fields). The docs report ``input_tokens`` and ``cache_read_tokens`` separately, so this
    adapter does **not** subtract cached tokens from input; whether ``input_tokens`` already
    includes cache reads is unverified **[verify]** and must be re-checked by a canary before
    cost settlement (section 8.5). How thinking tokens are billed is likewise undocumented
    **[verify]**.
    """
    raw = payload.get("usage")
    usage: dict[str, Any] = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    return Usage.model_validate(
        {
            "duration_s": proc.duration_s or None,
            "tokens_in": _int(usage.get("input_tokens")),
            "tokens_out": _int(usage.get("output_tokens")),
            "cache_read_tokens": _int(usage.get("cache_read_tokens")),
            "thinking_tokens": _int(usage.get("thinking_tokens")),
        }
    )
