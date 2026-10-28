"""Claude Code adapter (design sections 7.2, 7.3, 9.3, 9.4).

Task 1.9. Translates a task spec into a `claude -p` invocation and parses the JSON envelope
back into a result spec. Permission enforcement and environment handling follow the design:

- ``full`` maps to ``--dangerously-skip-permissions``; ``readonly`` maps to
  ``--permission-mode plan``; ``workspace`` has no verified profile yet (task 1.20), so it
  fails closed instead of running unconfined.
- ``--bare`` is added only when Anthropic API-key auth is configured (``ANTHROPIC_API_KEY``
  in the parent environment or the agent's ``env:``), never for subscription/OAuth logins,
  and the detected mode is recorded by :meth:`probe`.
- The child environment is built with :func:`relay.core.env.build_env` (allowlist, section
  9.4) and passed as the *complete* environment (``Invocation.full_env``), so the parent
  environment never leaks wholesale.

The parsing order of section 7.3 and result validation live in the shared
:mod:`relay.adapters.parsing` module (task 1.11); this module keeps only the Claude-specific
native-payload extraction (``structured_output``) and enrichment (session id, model, usage).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from relay.core.coordinator import CoordinatorProfile, Qualification
from relay.core.env import build_env
from relay.core.platform.proc import CommandNotFound, ResolvedCommand, resolve_command, run_process
from relay.core.progress import ClaudeStreamParser, ProgressParser
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
from .coordination import (
    CoordinatorCall,
    CoordinatorUnavailable,
    coordinator_schema,
    parse_coordinator_object,
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

__all__ = ["AuthMode", "ClaudeAdapter", "PermissionUnsupported", "ResultInvalid"]

AuthMode = Literal["api-key", "subscription"]

# The only Anthropic auth key this adapter forwards from the parent environment (section
# 9.4: "variables each CLI needs for auth"). Everything else is dropped by the allowlist.
AUTH_KEYS: tuple[str, ...] = ("ANTHROPIC_API_KEY",)


# Tool-free coordinator profile (design 7.4; spike 2.31, Claude Code 2.1.310): no model tools,
# no MCP servers, no skills/commands and no user/project/local settings sources (so no
# startup hooks or CLAUDE.md-driven customization), and no persisted session. This is a
# candidate until a qualification record exists for the exact profile.
COORDINATOR_FLAGS: tuple[str, ...] = (
    "--tools",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--disable-slash-commands",
    "--setting-sources",
    "",
    "--no-session-persistence",
)


class PermissionUnsupported(ValueError):
    """A requested permission level has no verified Claude profile (design section 9.3)."""


def _bare_version(raw: str | None) -> str:
    """The version number alone, so a record matches `2.1.312 (Claude Code)` and `2.1.312`."""
    match = re.search(r"\d+(?:\.\d+)+", raw or "")
    return match.group(0) if match else "unknown"


def _auth_mode(parent: Mapping[str, str], agent: Agent) -> AuthMode:
    """Detect the Anthropic auth mode: ``api-key`` when ``ANTHROPIC_API_KEY`` is configured.

    Only an API key (or an apiKeyHelper, which this adapter does not detect yet) works under
    ``--bare``; a subscription/OAuth login lives in the keychain and is never read there
    (design section 7.2). The key may come from the parent environment or the agent's ``env:``.
    """
    if parent.get("ANTHROPIC_API_KEY") or agent.env.get("ANTHROPIC_API_KEY"):
        return "api-key"
    return "subscription"


def _auth_env(parent: Mapping[str, str]) -> dict[str, str]:
    """The known auth keys present in the parent, forwarded as per-invocation extras."""
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


def _claude_payload(stdout: str) -> dict[str, Any] | None:
    """Parse Claude's ``--output-format json`` result envelope from stdout, or None.

    The envelope is a single JSON object with ``"type": "result"``. It may be the whole
    stdout or one line among progress output, so a line scan is the fallback.
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


class ClaudeAdapter:
    id = "claude"

    def __init__(
        self,
        agent: Agent,
        *,
        cmd: str | None = None,
        parent_env: Mapping[str, str] | None = None,
        stream_progress: bool = False,
    ) -> None:
        self._stream_progress = stream_progress
        self._agent = agent
        self._cmd = cmd or agent.cmd
        self._parent_env: Mapping[str, str] = dict(os.environ) if parent_env is None else parent_env

    # --- AgentAdapter -----------------------------------------------------------------

    def reuse_fingerprint(self, task: TaskSpec) -> str | None:
        # Bare API-key mode bypasses mutable account/config discovery. Other explicit
        # environment overrides could select providers/helpers that have no such proof.
        if (
            not task.routing.model
            or not task.routing.model.strip()
            or task.routing.model == "default"
            or _auth_mode(self._parent_env, self._agent) != "api-key"
            or any(key != "ANTHROPIC_API_KEY" for key in self._agent.env)
        ):
            return None
        env = build_env(
            agent_config=self._agent,
            extra_env=_auth_env(self._parent_env),
            parent_env=self._parent_env,
        )
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        payload = json.dumps(
            [self._cmd, "bare", task.routing.model, task.routing.effort, task.permissions, env],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hmac.new(
            key.encode(), b"relay.execution-fingerprint.v1\0" + payload, "sha256"
        ).hexdigest()

    async def probe(self) -> Probe:
        parent = self._parent_env
        auth = _auth_mode(parent, self._agent)
        notes = (f"auth-mode={auth}",)
        env = build_env(agent_config=self._agent, parent_env=parent)
        try:
            resolved = resolve_command(self._cmd, ("--version",), env=env)
        except CommandNotFound:
            return Probe(available=False, levels=("full",), notes=(*notes, "command not found"))
        version = await asyncio.to_thread(self._run_version, resolved, env)
        if version is None:
            notes = (*notes, "--version produced no output")
        # `readonly` only where its canary returned `enforced` (task 2.30); never `workspace`.
        return Probe(
            available=True, version=version, levels=advertised_levels(self.id), notes=notes
        )

    def _run_version(self, resolved: ResolvedCommand, env: Mapping[str, str]) -> str | None:
        result = run_process(resolved, cwd=Path.cwd(), env=env)
        if result.exit_code != 0:
            return None
        text = (result.stdout or result.stderr).strip()
        return text.splitlines()[0] if text else None

    def progress_parser(self) -> ProgressParser | None:
        return ClaudeStreamParser() if self._stream_progress else None

    # --- coordinator capability (design 5.6, 7.4) ------------------------------------

    def coordinator_args(self, *, model: str | None = None, effort: str | None = None) -> list[str]:
        """The argv (after the command) of one tool-free coordinator call; prompt on stdin."""
        args = ["-p", "--output-format", "json"]
        args += ["--json-schema", json.dumps(coordinator_schema(), separators=(",", ":"))]
        args += list(COORDINATOR_FLAGS)
        if model and model != "default":
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        return args

    def coordinator_profile(self, cli_version: str | None) -> CoordinatorProfile:
        """The exact profile qualification is specific to (section 7.4).

        The digest covers the restriction flags, the command and the names of configured
        environment overrides. It cannot see managed policy or helpers the CLI loads on its
        own, which is why qualification stays an opt-in, reviewed record.
        """
        canonical = json.dumps(
            {
                "flags": list(COORDINATOR_FLAGS),
                "cmd": self._cmd,
                "env_keys": sorted(self._agent.env),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return CoordinatorProfile(
            agent=self.id,
            cli_version=_bare_version(cli_version),
            os=sys.platform,
            auth_mode=_auth_mode(self._parent_env, self._agent),
            config_digest=hashlib.sha256(canonical.encode()).hexdigest(),
        )

    async def coordinate(
        self,
        prompt: str,
        *,
        cwd: Path,
        qualification: Qualification,
        model: str | None = None,
        effort: str | None = None,
        timeout_s: float | None = None,
    ) -> CoordinatorCall:
        """One bounded call returning a validated response; fails closed when unqualified.

        Raises :class:`CoordinatorUnavailable` unless `qualification` is a qualified record for
        exactly this CLI version, OS, auth mode and configuration, and
        :class:`~relay.adapters.coordination.CoordinatorInvalid` for a malformed response
        (the controller decides on its single schema repair).
        """
        env = build_env(
            agent_config=self._agent,
            extra_env=_auth_env(self._parent_env),
            parent_env=self._parent_env,
        )
        try:
            resolved = resolve_command(self._cmd, ("--version",), env=env)
        except CommandNotFound as exc:
            raise CoordinatorUnavailable(f"`{self._cmd}` not found on PATH") from exc
        version = await asyncio.to_thread(self._run_version, resolved, env)
        profile = self.coordinator_profile(version)
        if qualification.status != "qualified" or qualification.profile != profile:
            raise CoordinatorUnavailable(
                f"no qualified coordinator profile for claude {profile.cli_version} on "
                f"{profile.os} ({profile.auth_mode}); coordination is unavailable"
            )
        call = resolve_command(
            self._cmd, self.coordinator_args(model=model, effort=effort), env=env
        )
        proc = await asyncio.to_thread(
            run_process, call, cwd=cwd, env=env, stdin=prompt, timeout_s=timeout_s
        )
        payload = _claude_payload(proc.stdout)
        candidate: object = self._native(payload)
        if candidate is ABSENT and payload is not None:
            candidate = self._stdout_block(proc.stdout, payload)
        response = parse_coordinator_object(None if candidate is ABSENT else candidate, proc)
        usage = _usage(payload, proc) if payload is not None else None
        return CoordinatorCall(
            response=response,
            agent=self.id,
            model=_model(payload) if payload is not None else None,
            cli_version=version,
            profile_digest=profile.config_digest,
            schema_version=response.spec_version,
            duration_s=proc.duration_s,
            tokens_in=usage.tokens_in if usage else None,
            tokens_out=usage.tokens_out if usage else None,
            cost_usd=usage.cost_usd if usage else None,
        )

    def build_invocation(self, task: TaskSpec, ctx: RunContext) -> Invocation:
        parent = self._parent_env
        # Streaming and structured output qualify together: the stream ends with the same
        # `result` envelope, which `parse_result` already extracts line by line.
        args = (
            ["-p", "--output-format", "stream-json", "--verbose"]
            if self._stream_progress
            else [
                "-p",
                "--output-format",
                "json",
            ]
        )
        args += ["--json-schema", json.dumps(self._result_schema(), separators=(",", ":"))]
        model = task.routing.model
        if model and model != "default":
            args += ["--model", model]
        effort = task.routing.effort
        if effort:
            args += ["--effort", effort]
        args += self._permission_args(ctx.permission)
        if _auth_mode(parent, self._agent) == "api-key":
            args += ["--bare"]
        if ctx.resume_session:
            args += ["--resume", ctx.resume_session]
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
            stdin=task_prompt(task) + "\n",
        )

    @staticmethod
    def _result_schema() -> dict[str, Any]:
        """The generated result-spec schema, inline, without the ``$schema`` key (5.4)."""
        return claude_schema()

    @staticmethod
    def _permission_args(permission: PermissionLevel) -> list[str]:
        if permission == "full":
            return ["--dangerously-skip-permissions"]
        if permission == "readonly":
            return ["--permission-mode", "plan"]
        raise PermissionUnsupported(
            f"permission level {permission!r} has no verified Claude profile yet; "
            "refusing to run unconfined (design section 9.3)"
        )

    async def parse_result(
        self, proc: ProcessResult, task: TaskSpec, ctx: RunContext
    ) -> ResultSpec:
        payload = _claude_payload(proc.stdout)
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
        # Claude echoes the validated object as JSON in `result`; use it when no fenced block
        # exists (e.g. a schema-less run prompted to return JSON, as Cursor does).
        if payload is not None:
            result = payload.get("result")
            if isinstance(result, str) and result.strip().startswith("{"):
                try:
                    return json.loads(result)
                except json.JSONDecodeError:
                    return ABSENT
        return ABSENT

    @staticmethod
    def _synthesized(
        proc: ProcessResult, payload: dict[str, Any] | None, ctx: RunContext
    ) -> dict[str, Any]:
        ok = proc.exit_code == 0 and not proc.timed_out
        summary = ""
        if payload is not None and isinstance(payload.get("result"), str):
            summary = payload["result"].strip()
        else:
            lines = [line for line in proc.stdout.splitlines() if line.strip()]
            summary = lines[-1] if lines else ""
        return {
            "id": ctx.node_id,
            "status": "done" if ok else "failed",
            "agent": "claude",
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
            strip_operational_fields(candidate), node_id=task.id, agent="claude"
        )
        return self._enrich(result, payload, proc)

    @staticmethod
    def _enrich(
        result: ResultSpec, payload: dict[str, Any] | None, proc: ProcessResult
    ) -> ResultSpec:
        # Operational fields are never agent-declared (sections 5.4, 8.5): they come from
        # the CLI envelope, or stay absent. A `structured_output` that reports its own
        # session id, model or usage must not be trusted.
        if payload is None:
            result.session = None
            result.model = None
            result.usage = None
            return result
        session_id = payload.get("session_id")
        result.session = (
            Session(cli_session_id=session_id)
            if isinstance(session_id, str) and session_id
            else None
        )
        result.model = _model(payload)
        result.usage = _usage(payload, proc)
        return result


def _model(payload: dict[str, Any]) -> str | None:
    """The model that actually ran, from the (single) key of ``modelUsage`` (design 7.3)."""
    model_usage = payload.get("modelUsage")
    if not isinstance(model_usage, dict):
        return None
    keys = list(cast("dict[str, Any]", model_usage))
    return keys[0] if keys else None


def _usage(payload: dict[str, Any], proc: ProcessResult) -> Usage:
    """Normalize Claude's disjoint token buckets (design section 8.5).

    Claude's ``usage.input_tokens`` already excludes cache reads and writes (verified in
    tests/test_recorded_outputs.py), so the buckets map one-to-one: uncached input, cached
    read, cache write, and output.
    """
    raw = payload.get("usage")
    usage: dict[str, Any] = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    cost = payload.get("total_cost_usd")
    return Usage.model_validate(
        {
            "duration_s": proc.duration_s or None,
            "tokens_in": _int(usage.get("input_tokens")),
            "tokens_out": _int(usage.get("output_tokens")),
            "cost_usd": float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None,
            "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
            "cache_write_tokens": _int(usage.get("cache_creation_input_tokens")),
        }
    )
