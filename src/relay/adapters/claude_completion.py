"""Candidate Claude tool-free completion backend; reviewed qualification is mandatory.

This is separate from ClaudeAdapter's repository worker and coordinator paths. The only
streaming exposed by this backend is buffered HTTP streaming after final JSON validation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from relay.adapters.claude import COORDINATOR_FLAGS
from relay.adapters.completion import (
    QUALIFICATIONS,
    CompletionError,
    CompletionInvalid,
    CompletionProfile,
    CompletionQualification,
    CompletionRequest,
    CompletionResult,
    CompletionSupport,
    CompletionTerminationError,
    CompletionUsage,
    ObservedModel,
)
from relay.core.env import CompleteEnv, build_env
from relay.core.platform.proc import (
    CommandNotFound,
    OutputLimitExceeded,
    ProcessCancelled,
    ProcessDrainError,
    ProcessResult,
    ResolvedCommand,
    cancel_processes_when,
    resolve_command,
    run_process,
    stream_output,
)
from relay.core.registry import Agent

_CONFIG_BYTES = 1024 * 1024

# Versioned, specifically observed CLI bookkeeping, not a blanket cache/config exclusion.
# Unknown fields and policy/auth/MCP/feature-cache values continue to affect qualification.
_GLOBAL_OBSERVATION_FIELDS = frozenset(
    {
        "numStartups",
        "tipsHistory",
        "tipLifetimeShownCounts",
        "tipsHistoryByCommand",
        "cachedGrowthBookFeaturesAt",
    }
)
_PROJECT_OBSERVATION_FIELDS = frozenset(
    {
        "lastAPIDuration",
        "lastAPIDurationWithoutRetries",
        "lastCost",
        "lastDuration",
        "lastFpsAverage",
        "lastFpsLow1Pct",
        "lastGracefulShutdown",
        "lastLinesAdded",
        "lastLinesRemoved",
        "lastModelUsage",
        "lastSessionId",
        "lastStartTime",
        "lastToolDuration",
        "lastTotalCacheCreationInputTokens",
        "lastTotalCacheReadInputTokens",
        "lastTotalInputTokens",
        "lastTotalOutputTokens",
    }
)
_PLUGIN_OBSERVATION_FIELDS = frozenset({"lastUsedAt", "lastUsedNumStartups", "usageCount"})


def _global_config_digest(data: bytes) -> str:
    try:
        parsed: Any = json.loads(data)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CompletionError(
            "CLI global configuration cannot be qualified", status=503, code="unavailable"
        ) from exc
    if not isinstance(parsed, dict):
        raise CompletionError(
            "CLI global configuration cannot be qualified", status=503, code="unavailable"
        )
    state = {
        key: value
        for key, value in cast("dict[str, Any]", parsed).items()
        if key not in _GLOBAL_OBSERVATION_FIELDS
    }
    projects = state.get("projects")
    if isinstance(projects, dict):
        # Keep every project identity and unknown/trust/MCP/permission field. A new project
        # entry can still invalidate qualification; never treat it as automatically harmless.
        state["projects"] = {
            key: {
                name: value
                for name, value in cast("dict[str, Any]", project).items()
                if name not in _PROJECT_OBSERVATION_FIELDS
            }
            if isinstance(project, dict)
            else project
            for key, project in cast("dict[str, Any]", projects).items()
        }
    plugins = state.get("pluginUsage")
    if isinstance(plugins, dict):
        kept: dict[str, Any] = {}
        for key, entry in cast("dict[str, Any]", plugins).items():
            if isinstance(entry, dict):
                entry = {
                    name: value
                    for name, value in cast("dict[str, Any]", entry).items()
                    if name not in _PLUGIN_OBSERVATION_FIELDS
                }
                if not entry:
                    continue
            kept[key] = entry
        if kept:
            state["pluginUsage"] = kept
        else:
            state.pop("pluginUsage")
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _capture(_: str) -> None:
    """Enable bounded capture without forwarding CLI display/progress as assistant content."""


VERSION_CACHE_TTL_S = 30.0
"""Reuse a `--version` probe this long while the resolved executable files are unchanged.

Spawning the CLI per request is the costly part of the profile check. An in-place upgrade
that keeps every resolved file's mtime and size is noticed within this TTL at the latest.
"""


def _executable_stamps(resolved: ResolvedCommand) -> tuple[tuple[str, int, int], ...]:
    """Identity of the absolute files on the resolved command line (executable or shim)."""
    stamps: list[tuple[str, int, int]] = []
    for arg in resolved.argv:
        if not os.path.isabs(arg):
            continue
        try:
            info = os.stat(arg)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(info.st_mode):
            stamps.append((arg, info.st_mtime_ns, info.st_size))
    return tuple(stamps)


def _tools_unqualified() -> CompletionError:
    return CompletionError(
        "Tool output is not qualified", status=400, code="unsupported_parameter", param="tools"
    )


class ClaudeCompletionAdapter:
    id = "claude"

    def __init__(
        self,
        agent: Agent,
        *,
        parent_env: Mapping[str, str] | None = None,
        qualifications: Sequence[CompletionQualification] | None = None,
    ) -> None:
        self._agent = agent
        self._parent_env = None if parent_env is None else dict(parent_env)
        self._qualifications = tuple(QUALIFICATIONS if qualifications is None else qualifications)
        self._version_lock = threading.Lock()
        self._version_cache: tuple[object, float, str] | None = None

    def _environment(self) -> CompleteEnv:
        parent = os.environ if self._parent_env is None else self._parent_env
        # User-configured credentials take precedence over inherited credentials.
        extra: dict[str, str] = {}
        if "ANTHROPIC_API_KEY" not in self._agent.env and parent.get("ANTHROPIC_API_KEY"):
            extra["ANTHROPIC_API_KEY"] = parent["ANTHROPIC_API_KEY"]
        return build_env(self._agent, extra, parent_env=parent)

    def _version(
        self, resolved: ResolvedCommand, env: CompleteEnv, cwd: Path, timeout_s: float
    ) -> str:
        """The CLI version, probed at most once per TTL for an unchanged executable/env."""
        key = (
            resolved.argv,
            resolved.windows_command_line,
            tuple(sorted(env.items())),
            _executable_stamps(resolved),
        )
        with self._version_lock:
            cached = self._version_cache
            if cached is not None and cached[0] == key and time.monotonic() < cached[1]:
                return cached[2]
        with stream_output(_capture, max_bytes=65536):
            result = run_process(resolved, cwd=cwd, env=env, timeout_s=min(10, timeout_s))
        if result.timed_out:
            raise CompletionError("CLI version probe timed out", status=504, code="timeout")
        version = re.search(r"\b\d+(?:\.\d+)+\b", result.stdout or result.stderr)
        if result.exit_code or version is None:
            raise CompletionError(
                "Claude CLI version is unavailable", status=503, code="unavailable"
            )
        with self._version_lock:
            self._version_cache = (key, time.monotonic() + VERSION_CACHE_TTL_S, version.group(0))
        return version.group(0)

    def _profile(self, env: CompleteEnv, cwd: Path, timeout_s: float) -> CompletionProfile:
        resolved = resolve_command(self._agent.cmd, ("--version",), env=env, cwd=cwd)
        version = self._version(resolved, env, cwd, timeout_s)
        # Configuration is cheap to hash and is re-read on every call: any content change,
        # however quick or same-sized, still invalidates qualification before launch.
        home = Path(env.get("USERPROFILE") or env.get("HOME") or str(Path.home()))
        config = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        paths = (config / "settings.json", config / "settings.local.json", home / ".claude.json")
        files: dict[str, str] = {}
        for path in paths:
            try:
                with path.open("rb") as handle:
                    data = handle.read(_CONFIG_BYTES + 1)
                if len(data) > _CONFIG_BYTES:
                    raise CompletionError(
                        "CLI configuration is too large to qualify", status=503, code="unavailable"
                    )
                files[str(path.resolve())] = (
                    _global_config_digest(data)
                    if path == home / ".claude.json"
                    else hashlib.sha256(data).hexdigest()
                )
            except FileNotFoundError:
                files[str(path.absolute())] = "absent"
            except OSError as exc:
                raise CompletionError(
                    "CLI configuration cannot be qualified", status=503, code="unavailable"
                ) from exc
        # Values (including config-home/provider overrides) participate in identity, but only
        # the final aggregate digest leaves this function. No env/config content is reported.
        canonical = json.dumps(
            {
                "protocol": "relay-completion-v1-buffered-json",
                "profile_revision": 2,
                "command": self._agent.cmd,
                "resolved": list(resolved.argv),
                "flags": ["-p", "--output-format", "json", *COORDINATOR_FLAGS],
                "structured_mode": "--json-schema (validated client envelope schema)",
                "env": dict(env),
                "config_files": files,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return CompletionProfile(
            self.id,
            version,
            sys.platform,
            "api-key" if env.get("ANTHROPIC_API_KEY") else "subscription",
            hashlib.sha256(canonical.encode()).hexdigest(),
        )

    def profile(self, *, timeout_s: float = 10) -> CompletionProfile:
        """Bounded, non-billable profile discovery for opt-in qualification review."""
        try:
            with tempfile.TemporaryDirectory(prefix="relay-completion-probe-") as temp:
                return self._profile(self._environment(), Path(temp), timeout_s)
        except (CommandNotFound, OutputLimitExceeded) as exc:
            raise CompletionError(
                "Claude CLI is unavailable", status=503, code="unavailable"
            ) from exc
        except ProcessDrainError as exc:
            raise CompletionTerminationError() from exc

    def inspect(self, *, timeout_s: float = 10) -> CompletionSupport | None:
        try:
            profile = self.profile(timeout_s=timeout_s)
        except CompletionError:
            return None
        return self._support(profile)

    def _support(self, profile: CompletionProfile) -> CompletionSupport | None:
        for qualification in self._qualifications:
            if qualification.profile == profile and qualification.evidence.strip():
                return CompletionSupport(profile, supports_tools=qualification.supports_tools)
        return None

    @staticmethod
    def arguments(request: CompletionRequest) -> tuple[str, ...]:
        args = ["-p", "--output-format", "json", *COORDINATOR_FLAGS]
        if request.output_schema is not None:
            args += ["--json-schema", json.dumps(request.output_schema, separators=(",", ":"))]
        if request.model != "default":
            args += ["--model", request.model]
        if request.effort is not None:
            args += ["--effort", request.effort]
        return tuple(args)

    def complete(
        self,
        request: CompletionRequest,
        *,
        cwd: Path,
        cancel: threading.Event,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CompletionResult:
        deadline = time.monotonic() + timeout_s
        # Parameter checks that need no process run first: never spawn a probe to refuse.
        if request.max_tokens is not None:
            raise CompletionError(
                "Native token limits are not qualified",
                status=400,
                code="unsupported_parameter",
                param="max_tokens",
            )
        if request.output_schema is not None and not any(
            qualification.supports_tools for qualification in self._qualifications
        ):
            raise _tools_unqualified()
        try:
            with cancel_processes_when(cancel), stream_output(_capture, max_bytes=max_output_bytes):
                env = self._environment()
                support = self._support(self._profile(env, cwd, timeout_s))
                if support is None:
                    raise CompletionError(
                        "Claude completion profile is not qualified", status=503, code="unqualified"
                    )
                if request.output_schema is not None and not support.supports_tools:
                    raise _tools_unqualified()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CompletionError("Completion deadline expired", status=504, code="timeout")
                command = resolve_command(
                    self._agent.cmd, self.arguments(request), env=env, cwd=cwd
                )
                proc = run_process(
                    command, cwd=cwd, env=env, stdin=request.prompt, timeout_s=remaining
                )
            return parse_completion(proc, structured=request.output_schema is not None)
        except CommandNotFound as exc:
            raise CompletionError(
                "Claude CLI is unavailable", status=503, code="unavailable"
            ) from exc
        except ProcessCancelled as exc:
            raise CompletionError(
                "Completion cancelled", status=503, code="request_cancelled"
            ) from exc
        except OutputLimitExceeded as exc:
            raise CompletionError(
                "Completion output limit exceeded", code="output_limit_exceeded"
            ) from exc
        except ProcessDrainError as exc:
            raise CompletionTerminationError() from exc


def _usage(payload: dict[str, Any]) -> CompletionUsage | None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    raw = cast("dict[str, Any]", raw)
    values: list[int] = []
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values.append(value)
    inputs, outputs, reads, writes = values
    return CompletionUsage(inputs + reads + writes, outputs, reads, writes)


def _positive(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _observed(payload: dict[str, Any]) -> ObservedModel | None:
    """The model that ran and the limits the CLI reported for it, from ``modelUsage``.

    Only an unambiguous single entry with positive integer limits counts; anything else
    is reported as unknown rather than guessed.
    """
    raw = payload.get("modelUsage")
    if not isinstance(raw, dict) or len(cast("dict[str, Any]", raw)) != 1:
        return None
    ((model, entry),) = cast("dict[str, Any]", raw).items()
    if not model.strip() or not isinstance(entry, dict):
        return None
    entry = cast("dict[str, Any]", entry)
    context, output = _positive(entry.get("contextWindow")), _positive(entry.get("maxOutputTokens"))
    if context is None or output is None:
        return None
    return ObservedModel(model, context, output)


def parse_completion(proc: ProcessResult, *, structured: bool) -> CompletionResult:
    if proc.timed_out:
        raise CompletionError("Completion deadline expired", status=504, code="timeout")
    payload: dict[str, Any] | None = None
    candidates = [proc.stdout, *reversed(proc.stdout.splitlines())]
    for text in candidates:
        try:
            parsed: Any = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if isinstance(parsed, dict) and cast("dict[str, Any]", parsed).get("type") == "result":
            payload = cast("dict[str, Any]", parsed)
            break
    if proc.exit_code != 0 or payload is not None and payload.get("is_error"):
        # The category may use provider diagnostics; the diagnostic text never crosses HTTP.
        diagnostic = (proc.stderr + " " + (json.dumps(payload) if payload else "")).lower()
        if any(
            word in diagnostic
            for word in ("authentication", "unauthorized", "not logged in", "invalid api key")
        ):
            raise CompletionError(
                "CLI authentication is unavailable", status=503, code="authentication_unavailable"
            )
        if any(
            word in diagnostic
            for word in ("context_length_exceeded", "prompt is too long", "context window")
        ):
            raise CompletionError(
                "Context length exceeded",
                status=400,
                code="context_length_exceeded",
                param="messages",
            )
        raise CompletionError("Claude CLI completion failed")
    if payload is None:
        raise CompletionInvalid("CLI returned no completion object", raw=proc.stdout[:4000])
    usage = _usage(payload)
    observed = _observed(payload)
    length = payload.get("stop_reason") in ("max_tokens", "length")
    if structured:
        if length:
            raise CompletionError(
                "Tool output reached its output limit", code="incomplete_tool_output"
            )
        candidate: Any = payload.get("structured_output")
        if candidate is None and isinstance(payload.get("result"), str):
            with contextlib.suppress(ValueError, RecursionError):
                candidate = json.loads(payload["result"])
        if not isinstance(candidate, dict):
            raise CompletionInvalid(
                "CLI returned no tool envelope",
                raw=str(payload.get("result", ""))[:4000],
                usage=usage,
            )
        envelope = cast("dict[str, Any]", candidate)
        return CompletionResult(
            envelope=envelope, usage=usage, raw=json.dumps(envelope), observed=observed
        )
    content = payload.get("result")
    if not isinstance(content, str):
        raise CompletionInvalid("CLI returned no assistant text", usage=usage)
    try:
        content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CompletionError("CLI returned invalid Unicode text") from exc
    return CompletionResult(
        content=content,
        finish_reason="length" if length else "stop",
        usage=usage,
        observed=observed,
    )


def profile_json(adapter: ClaudeCompletionAdapter) -> str:
    """No model call and no qualification mutation; suitable for a review artifact."""
    return json.dumps(asdict(adapter.profile()), indent=2)
