"""Cursor CLI (`agent`, legacy alias `cursor-agent`) adapter (design 7.2, 7.3, 5.4, 9.3, 9.4).

Task 4.2. Translates a task spec into an ``agent -p`` invocation and parses the JSON envelope
back into a result spec.

Invocation shape:
- ``-p --output-format json --trust``. ``--trust`` accepts the (relay-created) worktree as
  a trusted workspace, so a headless run never stops at a trust prompt. The working
  directory is the process cwd (the node worktree), which is Cursor's default workspace.
- The prompt is piped on **stdin**, never placed on argv. With no positional prompt and a
  non-TTY stdin, ``agent`` reads and trims stdin as the prompt (``src/commands/build-prompt``
  in the 2026.10.22 bundle). On Windows the installed ``agent.cmd`` shim enables delayed
  expansion and forwards ``%*`` to a PowerShell 5.1 script that re-passes ``$args`` to
  ``node.exe``; prose on that argv path loses double quotes and ``!...!`` spans, while stdin
  passes through byte-exact. Only short flag tokens (model id, session id) travel on argv,
  and they are checked against a conservative character set first.
- No schema flag exists, so the generated result schema is injected into the prompt and
  the agent is asked to reply with the JSON object only (design 5.4). Cursor returned that
  JSON as text in the envelope's ``result`` field (spike 0.11), with ``options: []`` on free
  questions and ``null`` defaults; :func:`relay.adapters.parsing.normalize_result` removes both.
- ``--model <id>`` when the routed model is not the ``default`` sentinel. Reasoning effort
  has no verified mapping (Cursor uses bracket overrides such as ``model[effort=high]`` on
  some models), so ``routing.effort`` is not passed **[verify]**.
- Permission mapping (design 7.2 / 9.3): ``full`` -> ``--force`` (``--trust`` is always
  passed); ``readonly`` -> ``--mode plan`` without ``--force``, mapped but advertised only
  where :data:`relay.adapters.base.READONLY_VERIFIED_PLATFORMS` lists Cursor; ``workspace``
  fails closed. On Windows the 2026.10.22 CLI reports its sandbox helper as "proxy-only, no
  filesystem sandbox" and exits when ``--sandbox enabled`` is requested, so the candidate
  cannot confine writes there; it is unverified elsewhere. The 2026-10-27 doctor canaries
  (``tests/fixtures/recorded/cursor_doctor_canaries.json``) classified both restricted
  profiles ``inconclusive``.
- ``--resume <session_id>`` when ``ctx.resume_session`` is set (the ``session_id`` field and
  context retention were confirmed on 2026.10.19, design 7.3, and observed again by the
  session canary on 2026.10.22).

Command resolution: ``agent`` is a generic name, so when the configured command is the
built-in ``agent`` and it does not resolve, the legacy ``cursor-agent`` alias is tried.
Windows ``.cmd`` shims go through :func:`relay.core.platform.proc.resolve_command`.

Auth is the account login stored in the user profile (``USERPROFILE``/``APPDATA`` stay in
the Windows allowlist) or ``CURSOR_API_KEY``. The child environment is built with
:func:`relay.core.env.build_env` (allowlist, section 9.4) and passed as the complete
environment; ``CURSOR_API_KEY`` is the only auth key forwarded and is never logged.

Cursor is opt-in (``agents.cursor.enabled: false`` in the built-in defaults) and is not part
of the default foreground adapter registry, so an adapter module alone does not make it
routable. It has no ``coordinate`` capability: it is not a phase-2c coordinator or worker.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from relay.core.env import build_env
from relay.core.platform.proc import CommandNotFound, ResolvedCommand, resolve_command, run_process
from relay.core.registry import Agent
from relay.core.specs import ResultSpec, TaskSpec
from relay.core.specs.common import PermissionLevel
from relay.core.specs.result import Session, Usage

from .base import (
    Invocation,
    Probe,
    ProcessResult,
    RunContext,
    WorkerHandle,
    advertised_levels,
    task_prompt,
)
from .parsing import (
    ABSENT,
    ResultInvalid,
    claude_schema,
    node_file_source,
    parse_sources,
    stdout_block_source,
    strip_operational_fields,
    validate_result,
)

__all__ = [
    "AuthMode",
    "CursorAdapter",
    "LEGACY_COMMAND",
    "PermissionUnsupported",
    "ResultInvalid",
    "result_instructions",
    "schema_instructions",
]

AuthMode = Literal["api-key", "account-login"]

# The only auth key this adapter forwards from the parent environment (design 9.4).
AUTH_KEYS: tuple[str, ...] = ("CURSOR_API_KEY",)

DEFAULT_COMMAND = "agent"
LEGACY_COMMAND = "cursor-agent"

# The literal tier name meaning "pass no model flag" (design section 3).
_DEFAULT_MODEL = "default"

# Argv values that cross the Windows shim chain (cmd.exe with delayed expansion, then
# PowerShell 5.1): no quotes, `!`, `%`, `^`, whitespace or shell operators. Model ids such
# as `claude-opus-4-8[context=1m,effort=high]` and UUID session ids fit.
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+=,\[\]-]*")

# Cursor versions look like `2026.10.22-27dcf5f`.
_CURSOR_VERSION = re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}(?:-[0-9A-Za-z.-]+)?")

_PERMISSION_FLAGS: dict[PermissionLevel, tuple[str, ...]] = {
    "full": ("--force",),
    "readonly": ("--mode", "plan"),
}


class PermissionUnsupported(ValueError):
    """A requested permission level has no verified Cursor profile (design section 9.3)."""


def _auth_mode(parent: Mapping[str, str], agent: Agent) -> AuthMode:
    """``api-key`` when ``CURSOR_API_KEY`` is configured, else the stored account login."""
    if parent.get("CURSOR_API_KEY") or agent.env.get("CURSOR_API_KEY"):
        return "api-key"
    return "account-login"


def _auth_env(parent: Mapping[str, str]) -> dict[str, str]:
    """The known auth key present in the parent, forwarded as a per-invocation extra."""
    return {key: parent[key] for key in AUTH_KEYS if parent.get(key)}


def _safe_token(kind: str, value: str) -> str:
    if not _SAFE_TOKEN.fullmatch(value):
        raise ValueError(
            f"cursor {kind} {value!r} contains characters that do not survive the Windows "
            "shim chain; use an id made of letters, digits and ._:/@+=,[]-"
        )
    return value


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


def cursor_payload(stdout: str) -> dict[str, Any] | None:
    """Parse Cursor's ``--output-format json`` result envelope from stdout, or None.

    The envelope is one JSON object with ``"type": "result"`` (spike 0.11); a reverse line
    scan is the fallback in case other output precedes it.
    """
    text = stdout.strip()
    if not text:
        return None
    data = _try_json(text)
    if data is not None and data.get("type") == "result":
        return data
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        data = _try_json(line)
        if data is not None and data.get("type") == "result":
            return data
    return None


def schema_instructions(schema: Mapping[str, Any]) -> str:
    """Prompt text requesting one JSON object for `schema`, as the CLI has no schema flag."""
    return (
        "\n\nWhen you are finished, reply with only one JSON object (no prose and no code "
        "fence) that conforms to this JSON Schema:\n" + json.dumps(schema, separators=(",", ":"))
    )


def result_instructions() -> str:
    """The result-spec request appended to every task prompt (design 5.4)."""
    return (
        "\n\nReport the outcome as a relay result spec: status `done` when the task is "
        "complete, `failed` when it cannot be completed, or `needs_input` with typed "
        "`open_questions` when an operator decision is required that the repository cannot "
        "resolve. Do not report verification results; relay runs verification itself."
        # The compact result schema (no `$schema`, no titles) keeps the prompt small.
        + schema_instructions(claude_schema())
    )


class CursorAdapter:
    """The Cursor adapter. Translates, never decides routing or permissions (design 7.1)."""

    id = "cursor"

    def __init__(
        self,
        agent: Agent,
        *,
        cmd: str | None = None,
        parent_env: Mapping[str, str] | None = None,
        levels: tuple[PermissionLevel, ...] | None = None,
    ) -> None:
        self._agent = agent
        self._cmd = cmd or agent.cmd
        self._parent_env: Mapping[str, str] = dict(os.environ) if parent_env is None else parent_env
        # Default: what the canaries verified on this platform (design 9.3); `full` only
        # until Cursor appears in READONLY_VERIFIED_PLATFORMS.
        self._levels = levels if levels is not None else advertised_levels(self.id)

    # --- command resolution (design 7.2) ---------------------------------------------

    def command(self, env: Mapping[str, str] | None = None) -> str:
        """The command to launch: the configured one, or the legacy alias as a fallback.

        The alias is tried only for the built-in ``agent`` name; an explicitly configured
        command is never replaced. When neither resolves, the configured command is returned
        and launching it reports the missing executable.
        """
        lookup = env if env is not None else self._env()
        if self._cmd != DEFAULT_COMMAND:
            return self._cmd
        for candidate in (DEFAULT_COMMAND, LEGACY_COMMAND):
            try:
                resolve_command(candidate, (), env=lookup)
            except CommandNotFound:
                continue
            return candidate
        return self._cmd

    def _env(self) -> dict[str, str]:
        return build_env(
            agent_config=self._agent,
            extra_env=_auth_env(self._parent_env),
            parent_env=self._parent_env,
        )

    # --- AgentAdapter -----------------------------------------------------------------

    async def probe(self) -> Probe:
        notes: tuple[str, ...] = (f"auth-mode={_auth_mode(self._parent_env, self._agent)}",)
        env = build_env(agent_config=self._agent, parent_env=self._parent_env)
        cmd = self.command(env)
        if cmd != self._cmd:
            notes = (*notes, f"using legacy alias {cmd}")
        try:
            resolved = resolve_command(cmd, ("--version",), env=env)
        except CommandNotFound:
            return Probe(available=False, levels=self._levels, notes=(*notes, "command not found"))
        version = await asyncio.to_thread(self._run_version, resolved, env)
        if version is None:
            notes = (*notes, "--version produced no output")
        elif not _CURSOR_VERSION.search(version):
            notes = (*notes, "--version output does not look like a Cursor CLI version")
        return Probe(available=True, version=version, levels=self._levels, notes=notes)

    @staticmethod
    def _run_version(resolved: ResolvedCommand, env: Mapping[str, str]) -> str | None:
        result = run_process(resolved, cwd=Path.cwd(), env=env, timeout_s=30.0)
        if result.exit_code != 0:
            return None
        text = (result.stdout or result.stderr).strip()
        return text.splitlines()[0] if text else None

    def build_invocation(self, task: TaskSpec, ctx: RunContext) -> Invocation:
        args = ["-p", "--output-format", "json", "--trust"]
        model = task.routing.model
        if model and model != _DEFAULT_MODEL:
            args += ["--model", _safe_token("model", model)]
        args += self._permission_args(ctx.permission)
        if ctx.resume_session:
            args += ["--resume", _safe_token("session id", ctx.resume_session)]
        env = self._env()
        return Invocation(
            cmd=self.command(env),
            args=tuple(args),
            cwd=ctx.worktree,
            env=env,
            full_env=True,
            stdin=task_prompt(task) + result_instructions() + "\n",
        )

    def _permission_args(self, permission: PermissionLevel) -> list[str]:
        flags = _PERMISSION_FLAGS.get(permission)
        if flags is None:
            raise PermissionUnsupported(
                f"permission level {permission!r} has no verified Cursor profile yet; "
                "refusing to run unconfined (design section 9.3)"
            )
        return list(flags)

    async def parse_result(
        self, proc: ProcessResult, task: TaskSpec, ctx: RunContext
    ) -> ResultSpec:
        payload = cursor_payload(proc.stdout)
        return parse_sources(
            (
                # Step 1 (native, schema-constrained output) does not exist for Cursor.
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
    def _stdout_block(stdout: str, payload: dict[str, Any] | None) -> object:
        block = stdout_block_source(stdout)
        if block is not ABSENT:
            return block
        if payload is None:
            return ABSENT
        result = payload.get("result")
        if not isinstance(result, str):
            return ABSENT
        text = result.strip()
        if text.startswith("{"):
            data = _try_json(text)
            if data is not None:
                return data
        # A reply that wrapped the object in a fenced block despite the instructions.
        return stdout_block_source(result)

    @staticmethod
    def _synthesized(
        proc: ProcessResult, payload: dict[str, Any] | None, ctx: RunContext
    ) -> dict[str, Any]:
        ok = proc.exit_code == 0 and not proc.timed_out
        if payload is not None and payload.get("is_error") is True:
            ok = False
        summary = ""
        if payload is not None and isinstance(payload.get("result"), str):
            summary = payload["result"].strip()
        else:
            lines = [line for line in proc.stdout.splitlines() if line.strip()]
            summary = lines[-1] if lines else ""
        return {
            "id": ctx.node_id,
            "status": "done" if ok else "failed",
            "agent": "cursor",
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
        result = validate_result(
            strip_operational_fields(candidate), node_id=task.id, agent="cursor"
        )
        # Operational fields are never agent-declared (sections 5.4, 8.5): the session id
        # and usage come from the CLI envelope; the envelope names no model, so the routed
        # model is recorded (or nothing for the `default` sentinel).
        routed = task.routing.model
        result.model = routed if routed and routed != _DEFAULT_MODEL else None
        if payload is None:
            result.session = None
            result.usage = None
            return result
        session_id = payload.get("session_id")
        result.session = (
            Session(cli_session_id=session_id)
            if isinstance(session_id, str) and session_id
            else None
        )
        result.usage = _usage(payload, proc)
        return result


def _usage(payload: dict[str, Any], proc: ProcessResult) -> Usage:
    """Normalize Cursor's camelCase token counts (design 8.5; spike 0.11).

    ``inputTokens``, ``cacheReadTokens`` and ``cacheWriteTokens`` are disjoint buckets, so
    they map one-to-one: the recorded resume reports 4649 input plus 15716 cache-read tokens,
    about the 20201 uncached input tokens of the run it resumed. The envelope reports no
    cost; cost comes from configured pricing.
    """
    raw = payload.get("usage")
    usage: dict[str, Any] = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    return Usage.model_validate(
        {
            "duration_s": proc.duration_s or None,
            "tokens_in": _int(usage.get("inputTokens")),
            "tokens_out": _int(usage.get("outputTokens")),
            "cache_read_tokens": _int(usage.get("cacheReadTokens")),
            "cache_write_tokens": _int(usage.get("cacheWriteTokens")),
        }
    )
