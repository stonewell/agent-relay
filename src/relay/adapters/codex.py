"""`codex` adapter (design sections 7.1, 7.2, 7.3, 5.4, 9.3, 9.4).

The adapter translates a :class:`~relay.core.specs.task.TaskSpec` into a
``codex exec`` invocation and the CLI's ``--json`` JSONL output back into a
:class:`~relay.core.specs.result.ResultSpec`.

Invocation shape (design 7.2): the prompt is piped to stdin with ``-``, never placed on
the command line; ``--json`` turns on JSONL events; ``--output-schema <file>`` requests
structured output with the strict-form conversion of section 5.4; ``-o <file>`` captures
the last message; ``-C <worktree>`` sets the working directory. The router owns worktrees,
so ``--worktree`` is never used, and the top-level ``-a/--ask-for-approval`` flag does not
exist on ``exec`` (design 7.2), so approval is set through the sandbox/bypass flags only.

Parsing (design 7.3): the JSONL stream carries the session id in ``thread.started``, the
text in ``item.completed`` events (and the ``-o`` file), and usage in ``turn.completed``.
Codex's ``input_tokens`` include cached tokens, so the adapter subtracts them before
recording the uncached input count (section 8.5). The four-step parsing order and result
validation live in :mod:`relay.adapters.parsing` (task 1.11); this module keeps only the
Codex-specific native-payload extraction (the ``-o`` file or the last agent message).
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any, cast

from relay.core.env import AgentEnv, build_env
from relay.core.platform.proc import ProcessResult
from relay.core.progress import CodexEventParser, ProgressParser
from relay.core.specs import ResultSpec, TaskSpec, schema_for
from relay.core.specs.common import PermissionLevel
from relay.core.specs.result import Session, Usage
from relay.core.store import read_text

from .base import Invocation, RunContext, WorkerHandle, task_prompt
from .parsing import (
    ABSENT,
    ResultInvalid,
    drop_nulls,
    node_file_source,
    parse_sources,
    stdout_block_source,
    strict_schema,
    strip_operational_fields,
    validate_result,
)

__all__ = ["CodexAdapter", "ResultInvalid", "drop_nulls", "strict_schema"]

# Codex reads the prompt from stdin with `-` (design 7.2).
_STDIN_PROMPT = "-"

# Node-dir scratch files: the strict schema the adapter writes for `--output-schema` and
# the last-message file Codex writes for `-o` (design 6: agents write only their node dir).
_SCHEMA_FILE = "codex-schema.json"
_OUTPUT_FILE = "codex-output.json"

# Reasoning-effort config key (design 4.3: `-c model_reasoning_effort=<level>`). The key
# name is **[verify]** in the design and is not shown by `codex exec --help`, so it lives
# in one place for the `relay doctor` canary (task 1.12) to change if the real key differs.
_EFFORT_CONFIG_KEY = "model_reasoning_effort"

# The literal tier name for agents whose model ids are unverified (design 3): "pass no
# model flag, use the CLI's own configured model".
_DEFAULT_MODEL = "default"

# Auth keys Codex needs, passed only when present in the parent environment (design 9.4).
_CODEX_AUTH_KEYS: tuple[str, ...] = ("OPENAI_API_KEY", "CODEX_API_KEY")

# Permission flag mapping (design 7.2). `full` and `readonly` are accepted; `workspace` is
# mapped but rejected by `build_invocation`, so the router fails closed before dispatch.
_PERMISSION_FLAGS: dict[PermissionLevel, tuple[str, ...]] = {
    "full": ("--dangerously-bypass-approvals-and-sandbox",),
    "readonly": ("-s", "read-only"),
    "workspace": ("-s", "workspace-write"),
}


class CodexAdapter:
    """The ``codex`` adapter. Translates, never decides routing or permissions (design 7.1)."""

    id = "codex"

    def __init__(
        self,
        cmd: str = "codex",
        *,
        agent: AgentEnv | None = None,
        levels: tuple[PermissionLevel, ...] | None = None,
    ) -> None:
        self.cmd = cmd
        self._agent = agent
        # Default: `full` and `readonly` are both advertised; `workspace` still fails closed.
        self._levels = levels if levels is not None else ("full", "readonly")

    def progress_parser(self) -> ProgressParser | None:
        """`codex exec --json` is already a public JSONL stream; no invocation change needed."""
        return CodexEventParser()

    # --- AgentAdapter ---------------------------------------------------------------------

    def build_invocation(self, task: TaskSpec, ctx: RunContext) -> Invocation:
        if ctx.permission not in self._levels:
            raise ValueError(
                f"codex cannot honour permission level {ctx.permission!r} "
                f"(advertised levels: {', '.join(self._levels)})"
            )
        node_dir = ctx.node_dir
        node_dir.mkdir(parents=True, exist_ok=True)
        schema_file = node_dir / _SCHEMA_FILE
        schema_file.write_text(json.dumps(strict_schema(schema_for(ResultSpec))), encoding="utf-8")
        output_file = node_dir / _OUTPUT_FILE

        args: list[str] = ["exec"]
        if ctx.resume_session:
            args += ["resume", ctx.resume_session]
        args += [
            "--json",
            "--output-schema",
            str(schema_file),
            "-o",
            str(output_file),
            "-C",
            str(ctx.worktree),
        ]
        model = task.routing.model
        if model and model != _DEFAULT_MODEL:
            args += ["-m", model]
        effort = task.routing.effort
        if effort:
            args += ["-c", f"{_EFFORT_CONFIG_KEY}={effort}"]
        args += list(_PERMISSION_FLAGS[ctx.permission])
        args.append(_STDIN_PROMPT)

        env = build_env(agent_config=self._agent, extra_env=self._auth_env())
        return Invocation(
            cmd=self.cmd,
            args=tuple(args),
            cwd=ctx.worktree,
            env=env,
            full_env=True,
            stdin=task_prompt(task),
        )

    async def parse_result(
        self, proc: ProcessResult, task: TaskSpec, ctx: RunContext
    ) -> ResultSpec:
        events = self._parse_events(proc.stdout)
        thread_id = self._thread_id(events)
        usage = self._usage(events, proc)
        return parse_sources(
            (
                lambda: self._native(ctx, events),
                lambda: node_file_source(ctx),
                lambda: stdout_block_source(proc.stdout),
                lambda: self._synthesized(proc, task, self._last_message(events)),
            ),
            finalize=lambda candidate: self._finalize(candidate, task, thread_id, usage),
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        handle.kill_tree()

    # --- invocation helpers ---------------------------------------------------------------

    def _auth_env(self) -> dict[str, str]:
        """Only the known Codex auth keys, and only when set in the parent environment."""
        return {name: os.environ[name] for name in _CODEX_AUTH_KEYS if name in os.environ}

    # --- parsing --------------------------------------------------------------------------

    @staticmethod
    def _parse_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResultInvalid(f"malformed Codex JSONL event: {exc}") from exc
            if not isinstance(payload, dict):
                raise ResultInvalid("malformed Codex JSONL event: not an object")
            events.append(cast("dict[str, Any]", payload))
        return events

    @staticmethod
    def _thread_id(events: Sequence[dict[str, Any]]) -> str | None:
        for event in events:
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str):
                    return thread_id
        return None

    @staticmethod
    def _usage(events: Sequence[dict[str, Any]], proc: ProcessResult) -> dict[str, Any] | None:
        for event in reversed(events):
            if event.get("type") != "turn.completed":
                continue
            raw = event.get("usage")
            if not isinstance(raw, dict):
                continue
            usage_data = cast("dict[str, Any]", raw)
            input_tokens = usage_data.get("input_tokens")
            cached = usage_data.get("cached_input_tokens")
            output_tokens = usage_data.get("output_tokens")
            tokens_in = None
            if isinstance(input_tokens, int):
                tokens_in = max(0, input_tokens - (cached if isinstance(cached, int) else 0))
            return {
                "duration_s": proc.duration_s or None,
                "tokens_in": tokens_in,
                "tokens_out": output_tokens if isinstance(output_tokens, int) else None,
            }
        return None

    def _native(self, ctx: RunContext, events: Sequence[dict[str, Any]]) -> object:
        output_file = ctx.node_dir / _OUTPUT_FILE
        if output_file.is_file():
            try:
                return json.loads(read_text(output_file))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        text = self._last_message(events)
        if text is not None:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return ABSENT
            if isinstance(payload, dict):
                return cast("dict[str, Any]", payload)
        return ABSENT

    @staticmethod
    def _last_message(events: Sequence[dict[str, Any]]) -> str | None:
        """The text of the last agent message, if the stream produced one."""
        for event in reversed(events):
            if event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            message = cast("dict[str, Any]", item)
            if message.get("type") != "agent_message":
                continue
            text = message.get("text")
            if isinstance(text, str):
                return text
        return None

    @staticmethod
    def _finalize(
        candidate: object, task: TaskSpec, thread_id: str | None, usage: dict[str, Any] | None
    ) -> ResultSpec:
        result = validate_result(
            strip_operational_fields(candidate), node_id=task.id, agent="codex"
        )
        # Operational fields are never agent-declared (sections 5.4, 8.5): the session and
        # usage come from the CLI stream, the model from the routing decision, or they stay
        # absent. A structured output that reports its own model/session/usage is not trusted.
        routed = task.routing.model
        result.model = routed if routed and routed != _DEFAULT_MODEL else None
        result.session = Session(cli_session_id=thread_id) if thread_id else None
        result.usage = Usage.model_validate(usage) if usage is not None else None
        return result

    @staticmethod
    def _synthesized(proc: ProcessResult, task: TaskSpec, summary: str | None) -> dict[str, Any]:
        ok = proc.exit_code == 0 and not proc.timed_out
        return {
            "id": task.id,
            "status": "done" if ok else "failed",
            "agent": "codex",
            "summary": summary or "",
        }
