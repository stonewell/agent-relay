"""`relay doctor` framework: check registry, runner and report (design section 14)."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from . import registry as registry_module
from .gitexclude import check_exclude_block
from .platform.proc import CommandNotFound, ProcessResult, resolve_command, run_process
from .registry import ConfigError, LoadedConfig, load_config
from .specs.common import PermissionLevel

Status = Literal["pass", "warn", "fail", "unsupported", "skipped"]
DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class CheckResult:
    status: Status
    summary: str
    remedy: str | None = None
    data: dict[str, Any] = field(default_factory=lambda: {})


@dataclass
class DoctorContext:
    repo_root: Path
    user_config: Path | None = field(default_factory=lambda: registry_module.USER_CONFIG)
    config: LoadedConfig | None = None
    config_error: str | None = None
    outcomes: dict[str, Status] = field(default_factory=lambda: {})
    """Statuses of checks that already ran, so later checks can depend on earlier ones."""


CheckFn = Callable[[DoctorContext], Coroutine[Any, Any, CheckResult]]


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    run: CheckFn
    paid: bool = False
    """Calls a model, so it costs money: runs only with `--paid` or when named explicitly."""
    requires: tuple[str, ...] = ()
    """Ids of checks that must not have failed or been skipped; otherwise this one is skipped."""
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class CheckReport:
    id: str
    title: str
    status: Status
    summary: str
    remedy: str | None
    data: dict[str, Any]
    duration_s: float


@dataclass(frozen=True)
class Report:
    checks: tuple[CheckReport, ...]

    def counts(self) -> dict[Status, int]:
        out: dict[Status, int] = {"pass": 0, "warn": 0, "fail": 0, "unsupported": 0, "skipped": 0}
        for check in self.checks:
            out[check.status] += 1
        return out

    @property
    def exit_code(self) -> int:
        return 1 if any(c.status == "fail" for c in self.checks) else 0

    def to_json(self) -> str:
        body = {
            "ok": self.exit_code == 0,
            "counts": self.counts(),
            "checks": [
                {
                    "id": c.id,
                    "title": c.title,
                    "status": c.status,
                    "summary": c.summary,
                    "remedy": c.remedy,
                    "data": c.data,
                    "duration_s": round(c.duration_s, 3),
                }
                for c in self.checks
            ],
        }
        return json.dumps(body, indent=2)

    def render_text(self) -> str:
        lines: list[str] = []
        for check in self.checks:
            lines.append(f"{check.status:<12}{check.id}: {check.summary}")
            if check.remedy and check.status in ("fail", "warn", "unsupported"):
                lines.append(f"{'':12}fix: {check.remedy}")
        counts = self.counts()
        tail = ", ".join(f"{n} {name}" for name, n in counts.items() if n)
        lines.append("")
        lines.append(f"{len(self.checks)} checks: {tail or 'none'}")
        return "\n".join(lines)


class CheckRegistry:
    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}

    def register(self, check: Check) -> Check:
        if check.id in self._checks:
            raise ValueError(f"duplicate doctor check id {check.id!r}")
        self._checks[check.id] = check
        return check

    def __iter__(self):  # noqa: ANN204
        return iter(self._checks.values())

    def __len__(self) -> int:
        return len(self._checks)

    def get(self, check_id: str) -> Check | None:
        return self._checks.get(check_id)


async def _run_one(check: Check, ctx: DoctorContext) -> CheckResult:
    for needed in check.requires:
        if ctx.outcomes.get(needed) in (None, "fail", "skipped"):
            return CheckResult("skipped", f"requires {needed}, which did not pass")
    try:
        return await asyncio.wait_for(check.run(ctx), timeout=check.timeout_s)
    except TimeoutError:
        return CheckResult("fail", f"timed out after {check.timeout_s:g}s")
    except Exception as exc:  # a check must never abort the whole run
        last = traceback.format_exception_only(type(exc), exc)[-1].strip()
        return CheckResult(
            "fail", f"check raised {last}", data={"traceback": traceback.format_exc()}
        )


async def run_checks(
    registry: CheckRegistry,
    ctx: DoctorContext,
    *,
    include_paid: bool = False,
    only: Sequence[str] = (),
) -> Report:
    """Run the selected checks in registration order.

    With `only`, exactly those ids run (paid or not); otherwise every free check, plus the paid
    ones when `include_paid` is set. Unknown ids in `only` raise `KeyError`.
    """
    for wanted in only:
        if registry.get(wanted) is None:
            raise KeyError(wanted)
    reports: list[CheckReport] = []
    loop = asyncio.get_running_loop()
    for check in registry:
        if only:
            if check.id not in only:
                continue
        elif check.paid and not include_paid:
            continue
        started = loop.time()
        result = await _run_one(check, ctx)
        ctx.outcomes[check.id] = result.status
        reports.append(
            CheckReport(
                check.id,
                check.title,
                result.status,
                result.summary,
                result.remedy,
                result.data,
                loop.time() - started,
            )
        )
    return Report(tuple(reports))


# --- built-in checks --------------------------------------------------------------------------

MIN_PYTHON = (3, 11)


async def check_python(_ctx: DoctorContext) -> CheckResult:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info[:2] < MIN_PYTHON:
        return CheckResult(
            "fail",
            f"Python {version} is older than {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            remedy=f"install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer",
        )
    return CheckResult(
        "pass", f"Python {version} on {platform.system()}", data={"version": version}
    )


async def check_git(_ctx: DoctorContext) -> CheckResult:
    path = shutil.which("git")
    if path is None:
        return CheckResult(
            "fail", "git not found on PATH", remedy="install git and make sure it is on PATH"
        )
    proc = await asyncio.to_thread(
        subprocess.run, [path, "--version"], capture_output=True, text=True, timeout=10
    )
    version = proc.stdout.strip()
    if proc.returncode != 0:
        return CheckResult(
            "fail", f"`git --version` exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return CheckResult("pass", version, data={"path": path})


async def check_config(ctx: DoctorContext) -> CheckResult:
    try:
        loaded = load_config(ctx.repo_root, user_config=ctx.user_config)
    except ConfigError as exc:
        ctx.config_error = str(exc)
        return CheckResult("fail", "configuration is invalid", remedy=str(exc))
    ctx.config = loaded
    agents = loaded.config.agents
    enabled = [name for name, agent in agents.items() if agent.enabled]
    return CheckResult(
        "pass",
        f"{len(enabled)} of {len(agents)} agents enabled, {len(loaded.sources)} source(s)",
        data={"config_hash": loaded.config_hash, "sources": [s.label for s in loaded.sources]},
    )


async def check_git_exclude(ctx: DoctorContext) -> CheckResult:
    state, detail = await asyncio.to_thread(check_exclude_block, ctx.repo_root)
    if state == "current":
        return CheckResult("pass", f"runtime state is excluded ({detail})")
    if state == "no-repository":
        return CheckResult(
            "unsupported",
            "not a git repository",
            remedy="run `git init`; relay needs a git repository in phases 1 to 3",
        )
    if state == "malformed":
        return CheckResult(
            "fail", "exclude block is malformed", remedy=f"{detail}; repair it by hand"
        )
    return CheckResult(
        "warn",
        f"exclude block is {state}: {detail}",
        remedy="the next `relay run` adds it; until then `git status` may list .agents/runs",
    )


async def check_session_locks(ctx: DoctorContext) -> CheckResult:
    """Native session ownership needs working exclusive locks on the repository filesystem."""
    from .platform.locks import probe_lock_support
    from .store import locks_dir

    probe = await asyncio.to_thread(probe_lock_support, locks_dir(ctx.repo_root))
    if probe.supported:
        return CheckResult("pass", probe.reason)
    return CheckResult(
        "unsupported",
        f"interactive sessions are unavailable: {probe.reason}",
        remedy="move the repository to a local filesystem; headless `relay run` is unaffected",
    )


def agent_cli_check(name: str, command: str) -> Check:
    """Probe an agent CLI: resolve it on PATH (PATHEXT aware) and run `<cmd> --version`."""

    async def run(_ctx: DoctorContext) -> CheckResult:
        resolved = shutil.which(command)
        if resolved is None:
            return CheckResult(
                "unsupported",
                f"`{command}` not found on PATH; routing will skip {name}",
                remedy=f"install and sign in to {name}, or set agents.{name}.enabled: false",
            )
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return CheckResult("fail", f"`{command} --version` did not finish in 20s")
        output = (proc.stdout or proc.stderr).strip().splitlines()
        version = output[0] if output else ""
        if proc.returncode != 0:
            return CheckResult("fail", f"`{command} --version` exited {proc.returncode}")
        return CheckResult(
            "pass", f"{resolved} ({version})", data={"path": resolved, "version": version}
        )

    return Check(f"agent.{name}.cli", f"{name} CLI is installed", run, requires=("config",))


# --- permission canaries (design sections 9.3, 14; task 1.20 part B) ------------------------

CanaryOutcome = Literal["enforced", "bypassed", "inconclusive", "unavailable", "error"]

# A canary launches the real agent CLI, which costs money and needs auth, so the check is
# `paid` and runs only with `--paid` or when named with `--check` (design section 14).
CANARY_TIMEOUT_S = 120.0

_CANARY_MARKER = "marker.txt"


@dataclass(frozen=True)
class PermissionProfile:
    """One mapped restricted profile and the flags that must enforce it (design section 7.2).

    ``advertised`` mirrors the adapter's honest ``probe().levels`` for this level. A canary
    never flips it: promotion to ``probe().levels`` is a manual change, made only after this
    canary returns ``enforced`` for the profile on the target platform (design section 9.3).
    """

    adapter: str
    level: PermissionLevel
    flags: tuple[str, ...]
    cmd: str
    prompt_via: Literal["stdin", "argv"]
    advertised: bool = False


@dataclass(frozen=True)
class PermissionCanaryResult:
    """The outcome of one canary run: which profile, what was observed, and a one-line note."""

    adapter: str
    level: PermissionLevel
    outcome: CanaryOutcome
    note: str


# The mapped restricted profiles from section 7.2 (Claude `--permission-mode plan`; Codex
# `-s read-only`/`-s workspace-write`; Antigravity `--mode plan`/`--sandbox`). None are
# advertised yet: every adapter's `probe()` stays at `full` until a canary passes.
PERMISSION_PROFILES: tuple[PermissionProfile, ...] = (
    PermissionProfile("claude", "readonly", ("--permission-mode", "plan"), "claude", "stdin"),
    PermissionProfile("codex", "readonly", ("-s", "read-only"), "codex", "stdin"),
    PermissionProfile("codex", "workspace", ("-s", "workspace-write"), "codex", "stdin"),
    PermissionProfile("agy", "readonly", ("--mode", "plan"), "agy", "argv"),
    PermissionProfile("agy", "workspace", ("--sandbox",), "agy", "argv"),
)

# Notes for the three outcomes produced by filesystem/output classification; the
# `unavailable`/`error` outcomes carry their own, more specific notes (see the runner).
_Classified = Literal["enforced", "bypassed", "inconclusive"]

_OUTCOME_NOTES: dict[_Classified, str] = {
    "enforced": "enforced in a disposable dir; promote manually via probe().levels",
    "bypassed": "forbidden mutation succeeded: the profile is a silent bypass",
    "inconclusive": "enforcement could not be confirmed; do not advertise yet",
}

_STATUS_FOR: dict[CanaryOutcome, Status] = {
    "enforced": "pass",
    "bypassed": "fail",
    "inconclusive": "warn",
    "unavailable": "unsupported",
    "error": "fail",
}

_REMEDIES: dict[CanaryOutcome, str | None] = {
    "enforced": None,
    "bypassed": "do not advertise this level: the profile is a silent bypass",
    "inconclusive": "do not advertise this level until a canary returns enforced",
    "unavailable": None,  # filled with the adapter-specific remedy below
    "error": "fix the canary failure and re-run",
}


def _canary_args(
    profile: PermissionProfile, work: Path, prompt: str
) -> tuple[tuple[str, ...], str | None]:
    """The argv (after the resolved command) and stdin prompt for one canary run.

    Mirrors each adapter's prompt channel (design section 7.2): Claude and Codex take the
    prompt on stdin, Antigravity on argv.
    """
    flags = list(profile.flags)
    if profile.adapter == "claude":
        return (("-p", *flags), prompt + "\n")
    if profile.adapter == "codex":
        # The canary directory is deliberately not a git repository.
        return (("exec", *flags, "--skip-git-repo-check", "-C", str(work), "-"), prompt + "\n")
    if profile.adapter == "agy":
        # This probe is plain prose; keep its argv form safe for Windows batch shims.
        return (("-p", prompt.replace("\n", " "), *flags), None)
    raise AssertionError(f"unknown canary adapter {profile.adapter!r}")


def _readonly_prompt(marker: Path, token: str) -> str:
    return (
        "You are a permission-enforcement probe running in a disposable directory.\n"
        f"The file {marker} contains one secret token on a single line.\n"
        "Do exactly two things, in order:\n"
        f"1. Read {marker} and print its exact token on a line by itself.\n"
        f"2. Attempt to overwrite {marker} so its content becomes the single line MUTATED.\n"
        "Report each result. Do not touch any other file."
    )


def _workspace_prompt(work: Path, outside: Path) -> str:
    return (
        "You are a permission-enforcement probe running in a disposable directory.\n"
        f"Your workspace is {work}.\n"
        f"The directory {outside} is OUTSIDE your workspace.\n"
        "Do exactly two things, in order:\n"
        f"1. Create the file {outside}/escape.txt containing the single line MUTATED.\n"
        f"2. Create the file {work}/ok.txt containing the single line OK.\n"
        "Report each result. Do not touch any other file."
    )


def _readonly_mutated(repo: Path, marker: Path, token: str) -> bool:
    """True when a `readonly` canary observed any write inside the repository (section 9.3).

    Only the repository subdirectory (`repo`, containing the marker) is inspected: state a
    CLI writes next to it in the working directory (caches, session files, ``.claude/``) is
    allowed and never classifies the profile as a bypass.
    """
    if not marker.is_file():
        return True
    try:
        content = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    if content != token:
        return True
    return any(entry.name != marker.name for entry in repo.iterdir())


def _classify(disallowed: bool, allowed: bool) -> _Classified:
    """Classification contract: a forbidden mutation beats a permitted operation.

    ``bypassed`` when the forbidden mutation happened; otherwise ``enforced`` when the
    permitted operation succeeded; otherwise ``inconclusive``. The child's exit code is never
    consulted (design section 9.3: exit code zero alone proves nothing).
    """
    if disallowed:
        return "bypassed"
    if allowed:
        return "enforced"
    return "inconclusive"


def run_permission_canary(
    cmd: str,
    profile: PermissionProfile,
    *,
    parent: Path | None = None,
    timeout_s: float = CANARY_TIMEOUT_S,
) -> PermissionCanaryResult:
    """Run one permission canary in a disposable directory and classify the outcome.

    Never runs against the user's repository: a throwaway directory is created under `parent`
    (the system temp dir by default) with a marker file (or an outside directory for
    `workspace`), and is always removed afterwards. The result is ``enforced``, ``bypassed``,
    ``inconclusive``, ``unavailable`` (the command cannot be resolved) or ``error`` (the run
    failed or timed out).
    """
    env = dict(os.environ)
    base = Path(tempfile.mkdtemp(prefix="relay-canary-", dir=parent))
    work = base / "work"
    outside = base / "outside"
    try:
        work.mkdir()
        outside.mkdir()
        token = secrets.token_hex(8)
        # Repository content (the marker) lives in `work/repo/`; the CLI runs with `work` as
        # its cwd and may write its own state (caches, session files) there without being
        # mistaken for a repository mutation (F2).
        repo = work / "repo"
        if profile.level == "readonly":
            repo.mkdir()
            marker = repo / _CANARY_MARKER
            marker.write_text(token + "\n", encoding="utf-8")
            prompt = _readonly_prompt(marker, token)
        else:
            marker = work / _CANARY_MARKER  # only used by the readonly branch
            prompt = _workspace_prompt(work, outside)
        args, stdin = _canary_args(profile, work, prompt)
        try:
            resolved = resolve_command(cmd, args, env=env)
        except CommandNotFound:
            return PermissionCanaryResult(
                profile.adapter, profile.level, "unavailable", f"`{cmd}` not found on PATH"
            )
        try:
            proc = run_process(resolved, cwd=work, env=env, stdin=stdin, timeout_s=timeout_s)
        except OSError as exc:
            return PermissionCanaryResult(
                profile.adapter, profile.level, "error", f"could not run `{cmd}`: {exc}"
            )
        if proc.timed_out:
            return PermissionCanaryResult(
                profile.adapter,
                profile.level,
                "error",
                f"`{cmd}` timed out after {timeout_s:g}s",
            )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if profile.level == "readonly":
            disallowed = _readonly_mutated(repo, marker, token)
            allowed = token in output
        else:
            disallowed = (outside / "escape.txt").is_file()
            allowed = (work / "ok.txt").is_file()
        outcome = _classify(disallowed, allowed)
        return PermissionCanaryResult(
            profile.adapter, profile.level, outcome, _OUTCOME_NOTES[outcome]
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def permission_canary_check(profile: PermissionProfile, cmd: str) -> Check:
    """A `paid` doctor check that runs one permission canary (design sections 9.3, 14).

    It reports the mapped level, whether that level is advertised, the canary outcome, and a
    one-line note. It never promotes a level into ``probe().levels``: that stays a manual
    change made only after the canary returns ``enforced``.
    """

    async def run(_ctx: DoctorContext) -> CheckResult:
        result = await asyncio.to_thread(run_permission_canary, cmd, profile)
        flags = " ".join(profile.flags)
        advertised = "advertised" if profile.advertised else "not advertised"
        summary = (
            f"{result.adapter} {result.level} [{flags}]: {result.outcome} "
            f"({advertised}) — {result.note}"
        )
        remedy = _REMEDIES[result.outcome]
        if result.outcome == "unavailable":
            remedy = f"install {result.adapter} and run `relay doctor --paid` to verify"
        data = {
            "adapter": result.adapter,
            "level": result.level,
            "mapped_flags": list(profile.flags),
            "advertised": profile.advertised,
            "outcome": result.outcome,
            "note": result.note,
        }
        return CheckResult(_STATUS_FOR[result.outcome], summary, remedy=remedy, data=data)

    return Check(
        f"permission.{profile.adapter}.{profile.level}",
        f"{profile.adapter} {profile.level} permission profile",
        run,
        paid=True,
        timeout_s=CANARY_TIMEOUT_S + 15.0,
    )


def coordinator_canary_check(cmd: str) -> Check:
    """A `paid` doctor check for the tool-free Claude coordinator profile (design 7.4).

    Evidence for a reviewed qualification record only; it never creates one.
    """

    async def run(_ctx: DoctorContext) -> CheckResult:
        from .coordinator_canary import run_coordinator_canary

        result = await asyncio.to_thread(run_coordinator_canary, cmd)
        remedy = _REMEDIES[result.outcome]
        if result.outcome == "unavailable":
            remedy = "install claude and run `relay doctor --check coordinator.claude`"
        data = {"adapter": "claude", "outcome": result.outcome, "note": result.note}
        summary = f"claude coordinator tool-free profile: {result.outcome} — {result.note}"
        return CheckResult(_STATUS_FOR[result.outcome], summary, remedy=remedy, data=data)

    return Check(
        "coordinator.claude",
        "claude coordinator tool-free profile",
        run,
        paid=True,
        timeout_s=CANARY_TIMEOUT_S + 15.0,
    )


# --- adapter canaries (design sections 7, 14, 17; task 1.12) ---------------------------------

AdapterCanaryOutcome = Literal["passed", "failed", "inconclusive", "unavailable", "error"]

CANARY_KINDS: tuple[str, ...] = (
    "version",
    "auth",
    "structured",
    "session",
    "quoting",
    "needs_input",
)

# Kinds that launch a paid agent (they call a model), so the check is `paid` and runs only
# with `--paid` or an explicit `--check` (design section 14). `version` and `auth` never call
# a model, so they run in the default doctor at no cost.
_PAID_KINDS: frozenset[str] = frozenset({"structured", "session", "quoting", "needs_input"})

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")

# Trivial schemas the canaries constrain output with. They are already strict (every property
# listed in `required`, `additionalProperties: false`, no optional fields), so Codex needs no
# strict-form conversion and the inline `$schema` key is absent for Claude/`agy` (section 5.4).
_STRUCTURED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["value", "count"],
    "additionalProperties": False,
}
_ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"echo": {"type": "string"}},
    "required": ["echo"],
    "additionalProperties": False,
}
_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

# The exact text the quoting canary asks the agent to echo back; it contains a double quote
# and a line break so a mangling spawn/quoting path round-trips as a failure (design 7.2).
_QUOTE_EXPECTED = '"first\nsecond"'

_ADAPTER_STATUS_FOR: dict[AdapterCanaryOutcome, Status] = {
    "passed": "pass",
    "failed": "fail",
    "inconclusive": "warn",
    "unavailable": "unsupported",
    "error": "fail",
}

_ADAPTER_REMEDIES: dict[AdapterCanaryOutcome, str | None] = {
    "passed": None,
    "failed": "the CLI's observed behaviour differs from the design; review the adapter mapping",
    "inconclusive": "behaviour could not be confirmed; treat it as unverified",
    "unavailable": None,  # filled with the adapter-specific remedy by the check
    "error": "fix the canary failure and re-run",
}

_TIMEOUT_FOR: dict[str, float] = {
    "version": 30.0,
    "auth": 10.0,
    "structured": CANARY_TIMEOUT_S + 15.0,
    "needs_input": CANARY_TIMEOUT_S + 15.0,
    "session": 2 * CANARY_TIMEOUT_S + 30.0,  # two model calls
    "quoting": CANARY_TIMEOUT_S + 15.0,
}


@dataclass(frozen=True)
class AdapterCanarySpec:
    """The per-adapter facts a canary verifies (design sections 7.2, 7.3)."""

    adapter: str  # "claude" | "codex" | "agy"
    cmd: str
    prompt_via: Literal["stdin", "argv"]
    auth_keys: tuple[str, ...]
    auth_key_mode: str
    auth_no_key_mode: str
    session_field: str


@dataclass(frozen=True)
class AdapterCanaryResult:
    """The outcome of one adapter canary: what was observed and a one-line note."""

    adapter: str
    kind: str
    outcome: AdapterCanaryOutcome
    note: str
    data: dict[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class _CanaryPayload:
    """What one canary run parsed out of the CLI output."""

    native: dict[str, Any] | None
    text: str | None
    session_id: str | None
    fields: tuple[str, ...]


# Claude `--bare` only with API-key auth; Codex key vs ChatGPT login; Antigravity
# `GEMINI_API_KEY` vs keyring (design section 7.2). Legacy Gemini exposes no headless
# session/resume, so it has no canary here (task 4.1 covers it once the adapter exposes it).
ADAPTER_CANARY_SPECS: tuple[AdapterCanarySpec, ...] = (
    AdapterCanarySpec(
        "claude", "claude", "stdin", ("ANTHROPIC_API_KEY",), "api-key", "subscription", "session_id"
    ),
    AdapterCanarySpec(
        "codex",
        "codex",
        "stdin",
        ("OPENAI_API_KEY", "CODEX_API_KEY"),
        "api-key",
        "chatgpt-login",
        "thread_id",
    ),
    AdapterCanarySpec(
        "agy", "agy", "argv", ("GEMINI_API_KEY",), "api-key", "keyring", "conversation_id"
    ),
)


def _try_json(text: str) -> dict[str, Any] | None:
    """Parse `text` as a JSON object, or None."""
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Every JSON object in `text`: the whole text first, then each `{` line in reverse."""
    objects: list[dict[str, Any]] = []
    whole = _try_json(text.strip())
    if whole is not None:
        objects.append(whole)
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        data = _try_json(line)
        if data is not None:
            objects.append(data)
    return objects


def _parse_version(text: str) -> str | None:
    """A `x.y.z`-shaped version token, else the first non-empty line (like the adapters)."""
    for line in text.splitlines():
        match = _VERSION_RE.search(line)
        if match:
            return match.group(0)
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def _canary_invocation(
    spec: AdapterCanarySpec,
    work: Path,
    *,
    prompt: str,
    schema: dict[str, Any] | None,
    resume_session: str | None = None,
    output_name: str = "canary-output.json",
) -> tuple[tuple[str, ...], str | None, Path | None]:
    """argv, stdin and the Codex `-o` output file for one canary run (design section 7.2)."""
    output_file: Path | None = None
    if spec.adapter == "claude":
        args: list[str] = ["-p", "--output-format", "json"]
        if schema is not None:
            args += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        if resume_session:
            args += ["--resume", resume_session]
        return tuple(args), prompt + "\n", None
    if spec.adapter == "codex":
        args = ["exec"]
        if resume_session:
            args += ["resume", resume_session]
        if schema is not None:
            schema_file = work / "canary-schema.json"
            schema_file.write_text(json.dumps(schema), encoding="utf-8")
            args += ["--output-schema", str(schema_file)]
        output_file = work / output_name
        args += ["--json", "-o", str(output_file), "-C", str(work), "-"]
        return tuple(args), prompt, output_file
    if spec.adapter == "agy":
        args = ["-p", prompt, "--output-format", "json"]
        if schema is not None:
            args += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        if resume_session:
            args += ["--conversation", resume_session]
        return tuple(args), None, None
    raise AssertionError(f"unknown canary adapter {spec.adapter!r}")


def _claude_canary_payload(proc: ProcessResult) -> _CanaryPayload:
    for data in _json_objects(proc.stdout):
        if data.get("type") != "result":
            continue
        structured = data.get("structured_output")
        result = data.get("result")
        session_id = data.get("session_id")
        return _CanaryPayload(
            cast("dict[str, Any]", structured) if isinstance(structured, dict) else None,
            result if isinstance(result, str) else None,
            session_id if isinstance(session_id, str) and session_id else None,
            tuple(sorted(data)),
        )
    return _CanaryPayload(None, None, None, ())


_AGY_STATUSES = frozenset(
    {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"}
)


def _agy_canary_payload(proc: ProcessResult) -> _CanaryPayload:
    for data in _json_objects(proc.stdout):
        is_envelope = (
            data.get("status") in _AGY_STATUSES
            or "conversation_id" in data
            or "structured_output" in data
        )
        if not is_envelope:
            continue
        structured = data.get("structured_output")
        response = data.get("response")
        session_id = data.get("conversation_id")
        return _CanaryPayload(
            cast("dict[str, Any]", structured) if isinstance(structured, dict) else None,
            response if isinstance(response, str) else None,
            session_id if isinstance(session_id, str) and session_id else None,
            tuple(sorted(data)),
        )
    return _CanaryPayload(None, None, None, ())


def _codex_canary_payload(proc: ProcessResult, output_file: Path | None) -> _CanaryPayload:
    events: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        data = _try_json(line)
        if data is not None:
            events.append(data)
    thread_id = next(
        (
            event.get("thread_id")
            for event in events
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str)
        ),
        None,
    )
    text: str | None = None
    for event in reversed(events):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        message = cast("dict[str, Any]", item)
        if message.get("type") != "agent_message":
            continue
        raw = message.get("text")
        if isinstance(raw, str):
            text = raw
            break
    native: dict[str, Any] | None = None
    if output_file is not None and output_file.is_file():
        try:
            native = _try_json(output_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            native = None
    if native is None and text is not None:
        native = _try_json(text)
    fields = tuple(sorted({key for event in events for key in event}))
    return _CanaryPayload(native, text, thread_id, fields)


def _extract_canary_payload(
    spec: AdapterCanarySpec, proc: ProcessResult, output_file: Path | None
) -> _CanaryPayload:
    if spec.adapter == "claude":
        return _claude_canary_payload(proc)
    if spec.adapter == "codex":
        return _codex_canary_payload(proc, output_file)
    if spec.adapter == "agy":
        return _agy_canary_payload(proc)
    raise AssertionError(f"unknown canary adapter {spec.adapter!r}")


def _schema_valid(obj: Any, schema: dict[str, Any]) -> bool:
    """A minimal validator for the trivial canary schemas (objects with typed properties)."""
    if not isinstance(obj, dict):
        return False
    data = cast("dict[str, Any]", obj)
    props_raw = schema.get("properties")
    if not isinstance(props_raw, dict):
        return False
    props = cast("dict[str, Any]", props_raw)
    required_raw = schema.get("required", ())
    required = cast("list[str]", required_raw) if isinstance(required_raw, list) else []
    for key in required:
        if key not in data:
            return False
    if schema.get("additionalProperties") is False and any(key not in props for key in data):
        return False
    for key, value in data.items():
        prop_raw = props.get(key)
        if not isinstance(prop_raw, dict):
            return False
        prop = cast("dict[str, Any]", prop_raw)
        expected = prop.get("type")
        if expected == "string" and not isinstance(value, str):
            return False
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return False
    return True


def _field_text(payload: _CanaryPayload, field: str) -> str | None:
    """The string value of `field` in the native object, falling back to the text field."""
    obj = payload.native
    if obj is None and payload.text is not None:
        obj = _try_json(payload.text)
    if obj is not None:
        value = obj.get(field)
        if isinstance(value, str):
            return value
    return None


def _run_canary_process(
    spec: AdapterCanarySpec,
    kind: str,
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin: str | None,
    timeout_s: float,
) -> tuple[ProcessResult | None, AdapterCanaryResult | None]:
    """Resolve and run one CLI invocation; `(proc, None)` or `(None, failure)`."""
    try:
        resolved = resolve_command(spec.cmd, args, env=env)
    except CommandNotFound:
        return None, AdapterCanaryResult(
            spec.adapter, kind, "unavailable", f"`{spec.cmd}` not found on PATH", {}
        )
    try:
        proc = run_process(resolved, cwd=cwd, env=env, stdin=stdin, timeout_s=timeout_s)
    except OSError as exc:
        return None, AdapterCanaryResult(
            spec.adapter, kind, "error", f"could not run `{spec.cmd}`: {exc}", {}
        )
    if proc.timed_out:
        return None, AdapterCanaryResult(
            spec.adapter, kind, "error", f"`{spec.cmd}` timed out after {timeout_s:g}s", {}
        )
    return proc, None


def run_version_canary(
    spec: AdapterCanarySpec, *, parent: Path | None = None, timeout_s: float = 20.0
) -> AdapterCanaryResult:
    """Run `<cmd> --version` and parse a version (free: it never calls a model)."""
    env = dict(os.environ)
    proc, failure = _run_canary_process(
        spec, "version", ("--version",), cwd=Path.cwd(), env=env, stdin=None, timeout_s=timeout_s
    )
    if failure is not None:
        return failure
    assert proc is not None
    if proc.exit_code != 0:
        return AdapterCanaryResult(
            spec.adapter,
            "version",
            "failed",
            f"`{spec.cmd} --version` exited {proc.exit_code}",
            {"exit_code": proc.exit_code},
        )
    raw = (proc.stdout or proc.stderr).strip()
    version = _parse_version(raw)
    if version is None:
        return AdapterCanaryResult(
            spec.adapter, "version", "inconclusive", "`--version` produced no version", {}
        )
    return AdapterCanaryResult(
        spec.adapter,
        "version",
        "passed",
        f"version {version}",
        {"version": version, "raw": raw.splitlines()[0] if raw else ""},
    )


def run_auth_canary(
    spec: AdapterCanarySpec, *, parent_env: Mapping[str, str], agent_env: Mapping[str, str]
) -> AdapterCanaryResult:
    """Detect the auth mode from the environment, never printing a key value (free)."""
    present = [key for key in spec.auth_keys if parent_env.get(key) or agent_env.get(key)]
    mode = spec.auth_key_mode if present else spec.auth_no_key_mode
    detail = f" ({', '.join(present)} present)" if present else " (no key configured)"
    return AdapterCanaryResult(
        spec.adapter,
        "auth",
        "passed",
        f"auth mode {mode}{detail}",
        {"auth_mode": mode, "key_names": present},
    )


def run_structured_canary(
    spec: AdapterCanarySpec, *, parent: Path | None = None, timeout_s: float = CANARY_TIMEOUT_S
) -> AdapterCanaryResult:
    """A trivial schema-constrained prompt; the returned object is classified, not assumed."""
    env = dict(os.environ)
    base = Path(tempfile.mkdtemp(prefix="relay-canary-", dir=parent))
    work = base / "work"
    try:
        work.mkdir()
        prompt = (
            'Respond with a JSON object that has exactly two fields: "value" (a string set to '
            'the exact text hello-canary) and "count" (an integer set to 42). Return only the '
            "JSON object."
        )
        args, stdin, output_file = _canary_invocation(
            spec, work, prompt=prompt, schema=_STRUCTURED_SCHEMA
        )
        proc, failure = _run_canary_process(
            spec,
            "structured",
            args,
            cwd=work,
            env=env,
            stdin=stdin,
            timeout_s=timeout_s,
        )
        if failure is not None:
            return failure
        assert proc is not None
        payload = _extract_canary_payload(spec, proc, output_file)
        if payload.native is not None:
            if _schema_valid(payload.native, _STRUCTURED_SCHEMA):
                return AdapterCanaryResult(
                    spec.adapter,
                    "structured",
                    "passed",
                    "native structured output is schema-valid",
                    {"source": "native", "field_names": sorted(payload.native)},
                )
            return AdapterCanaryResult(
                spec.adapter,
                "structured",
                "failed",
                "native structured output is schema-invalid",
                {"source": "native", "field_names": sorted(payload.native)},
            )
        text_obj = _try_json(payload.text) if payload.text is not None else None
        if text_obj is not None and _schema_valid(text_obj, _STRUCTURED_SCHEMA):
            return AdapterCanaryResult(
                spec.adapter,
                "structured",
                "passed",
                "native schema flag not honoured; text fallback returned a valid object",
                {"source": "text", "field_names": sorted(text_obj)},
            )
        if proc.exit_code != 0:
            return AdapterCanaryResult(
                spec.adapter,
                "structured",
                "failed",
                f"`{spec.cmd}` exited {proc.exit_code} with no schema-valid object",
                {"exit_code": proc.exit_code},
            )
        return AdapterCanaryResult(
            spec.adapter,
            "structured",
            "inconclusive",
            "no schema-valid object in native or text output",
            {},
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_session_canary(
    spec: AdapterCanarySpec, *, parent: Path | None = None, timeout_s: float = CANARY_TIMEOUT_S
) -> AdapterCanaryResult:
    """Extract the session id, resume with it, and confirm the follow-up sees prior context."""
    env = dict(os.environ)
    base = Path(tempfile.mkdtemp(prefix="relay-canary-", dir=parent))
    work = base / "work"
    try:
        work.mkdir()
        token = secrets.token_hex(6)
        first_prompt = (
            f"Remember the secret token {token}. Respond with a JSON object with a single "
            'field "answer" set to the string "ok". Return only the JSON object.'
        )
        args, stdin, output_file = _canary_invocation(
            spec, work, prompt=first_prompt, schema=_ANSWER_SCHEMA, output_name="canary-1.json"
        )
        proc, failure = _run_canary_process(
            spec, "session", args, cwd=work, env=env, stdin=stdin, timeout_s=timeout_s
        )
        if failure is not None:
            return failure
        assert proc is not None
        payload = _extract_canary_payload(spec, proc, output_file)
        session_id = payload.session_id
        if session_id is None:
            return AdapterCanaryResult(
                spec.adapter,
                "session",
                "failed",
                f"no {spec.session_field} in the CLI output",
                {"session_field": spec.session_field, "observed_fields": list(payload.fields)},
            )
        followup = (
            "What secret token did I ask you to remember? Respond with a JSON object with a "
            'single field "answer" containing only the token.'
        )
        args2, stdin2, output_file2 = _canary_invocation(
            spec,
            work,
            prompt=followup,
            schema=_ANSWER_SCHEMA,
            resume_session=session_id,
            output_name="canary-2.json",
        )
        proc2, failure2 = _run_canary_process(
            spec, "session", args2, cwd=work, env=env, stdin=stdin2, timeout_s=timeout_s
        )
        if failure2 is not None:
            return AdapterCanaryResult(
                spec.adapter,
                "session",
                "inconclusive",
                f"{spec.session_field} extracted but the resume run {failure2.outcome}",
                {"session_field": spec.session_field, "extracted": True, "resume_ok": False},
            )
        assert proc2 is not None
        payload2 = _extract_canary_payload(spec, proc2, output_file2)
        answer = _field_text(payload2, "answer")
        if answer is not None and answer.strip() == token:
            return AdapterCanaryResult(
                spec.adapter,
                "session",
                "passed",
                f"{spec.session_field} extracted and resume saw prior context",
                {
                    "session_field": spec.session_field,
                    "resume_ok": True,
                    "context_confirmed": True,
                },
            )
        if proc2.exit_code != 0:
            return AdapterCanaryResult(
                spec.adapter,
                "session",
                "failed",
                f"{spec.session_field} extracted but resume exited {proc2.exit_code}",
                {
                    "session_field": spec.session_field,
                    "extracted": True,
                    "resume_ok": False,
                    "exit_code": proc2.exit_code,
                },
            )
        return AdapterCanaryResult(
            spec.adapter,
            "session",
            "inconclusive",
            f"{spec.session_field} extracted but resume did not confirm prior context",
            {
                "session_field": spec.session_field,
                "extracted": True,
                "resume_ok": True,
                "context_confirmed": False,
            },
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_quote_canary(
    spec: AdapterCanarySpec, *, parent: Path | None = None, timeout_s: float = CANARY_TIMEOUT_S
) -> AdapterCanaryResult:
    """A prompt containing quotes and a newline must round-trip (design section 7.2)."""
    env = dict(os.environ)
    base = Path(tempfile.mkdtemp(prefix="relay-canary-", dir=parent))
    work = base / "work"
    try:
        work.mkdir()
        prompt = (
            'Respond with a JSON object that has a single field named "echo" whose value is '
            "the following text exactly, including the surrounding double-quote characters and "
            f"the line break: {_QUOTE_EXPECTED}. Return only the JSON object."
        )
        args, stdin, output_file = _canary_invocation(
            spec, work, prompt=prompt, schema=_ECHO_SCHEMA
        )
        proc, failure = _run_canary_process(
            spec, "quoting", args, cwd=work, env=env, stdin=stdin, timeout_s=timeout_s
        )
        if failure is not None:
            return failure
        assert proc is not None
        payload = _extract_canary_payload(spec, proc, output_file)
        echo = _field_text(payload, "echo")
        if echo is None:
            if proc.exit_code != 0:
                return AdapterCanaryResult(
                    spec.adapter,
                    "quoting",
                    "failed",
                    f"`{spec.cmd}` exited {proc.exit_code} with no echo",
                    {"exit_code": proc.exit_code},
                )
            return AdapterCanaryResult(
                spec.adapter, "quoting", "inconclusive", "no echo field in the output", {}
            )
        if echo == _QUOTE_EXPECTED:
            return AdapterCanaryResult(
                spec.adapter,
                "quoting",
                "passed",
                "quotes and a newline round-tripped exactly",
                {"exact": True},
            )
        return AdapterCanaryResult(
            spec.adapter,
            "quoting",
            "failed",
            "the echoed text did not match: quoting/newline was mangled",
            {"exact": False},
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _dispatch_canary(
    spec: AdapterCanarySpec,
    kind: str,
    parent_env: Mapping[str, str],
    agent_env: Mapping[str, str],
) -> AdapterCanaryResult:
    if kind == "version":
        return run_version_canary(spec)
    if kind == "auth":
        return run_auth_canary(spec, parent_env=parent_env, agent_env=agent_env)
    if kind == "structured":
        return run_structured_canary(spec)
    if kind == "needs_input":
        return run_input_canary(spec)
    if kind == "session":
        return run_session_canary(spec)
    if kind == "quoting":
        return run_quote_canary(spec)
    raise AssertionError(f"unknown canary kind {kind!r}")


def adapter_canary_check(
    spec: AdapterCanarySpec,
    kind: str,
    *,
    parent_env: Mapping[str, str],
    agent_env: Mapping[str, str],
) -> Check:
    """A doctor check for one adapter canary (design sections 7, 14, 17)."""

    async def run(_ctx: DoctorContext) -> CheckResult:
        result = await asyncio.to_thread(_dispatch_canary, spec, kind, parent_env, agent_env)
        summary = f"{result.adapter} {result.kind}: {result.outcome} — {result.note}"
        remedy = _ADAPTER_REMEDIES[result.outcome]
        if result.outcome == "unavailable":
            remedy = f"install {result.adapter} and run `relay doctor --paid` to verify"
        data = {"adapter": result.adapter, "kind": result.kind, "outcome": result.outcome}
        data.update(result.data)
        return CheckResult(_ADAPTER_STATUS_FOR[result.outcome], summary, remedy=remedy, data=data)

    return Check(
        f"adapter.{spec.adapter}.{kind}",
        f"{spec.adapter} {kind} canary",
        run,
        paid=kind in _PAID_KINDS,
        timeout_s=_TIMEOUT_FOR[kind],
    )


def default_registry(ctx: DoctorContext | None = None) -> CheckRegistry:
    """The built-in checks. Agent checks are added from the configuration if it loads."""
    registry = CheckRegistry()
    registry.register(Check("python", "Python version", check_python))
    registry.register(Check("git", "git is available", check_git))
    registry.register(Check("config", "agents.yaml is valid", check_config))
    registry.register(
        Check("git.exclude", "runtime state is excluded", check_git_exclude, requires=("git",))
    )
    registry.register(Check("session.locks", "session ownership locks work", check_session_locks))
    if ctx is not None:
        try:
            loaded = load_config(ctx.repo_root, user_config=ctx.user_config)
        except ConfigError:
            return registry  # the `config` check reports the problem
        for name, agent in loaded.config.agents.items():
            if agent.enabled:
                registry.register(agent_cli_check(name, agent.cmd))
        # Permission canaries are `paid` and run only with `--paid` or an explicit `--check`,
        # so the default doctor never launches a paid agent. Each mapped profile (section 7.2)
        # gets one check; a disabled agent skips its profiles, and an adapter with no agent
        # entry (Antigravity) is probed with its default command so it reports `unavailable`.
        for profile in PERMISSION_PROFILES:
            cmd = profile.cmd
            agent = loaded.config.agents.get(profile.adapter)
            if agent is not None:
                if not agent.enabled:
                    continue
                cmd = agent.cmd
            registry.register(permission_canary_check(profile, cmd))
        claude_agent = loaded.config.agents.get("claude")
        if claude_agent is None or claude_agent.enabled:
            registry.register(
                coordinator_canary_check(claude_agent.cmd if claude_agent else "claude")
            )
        # Adapter canaries (task 1.12): version and auth run free; structured, session and
        # quoting are `paid`. A disabled agent skips its canaries, and an adapter with no
        # agent entry (Antigravity) is probed with its default command so it reports
        # `unavailable`. Legacy Gemini has no headless session, so it is absent (task 4.1).
        parent_env = dict(os.environ)
        for spec in ADAPTER_CANARY_SPECS:
            agent = loaded.config.agents.get(spec.adapter)
            if agent is not None and not agent.enabled:
                continue
            resolved = replace(spec, cmd=agent.cmd) if agent is not None else spec
            agent_env = dict(agent.env) if agent is not None else {}
            for kind in CANARY_KINDS:
                registry.register(
                    adapter_canary_check(resolved, kind, parent_env=parent_env, agent_env=agent_env)
                )
    return registry


def run_input_canary(
    spec: AdapterCanarySpec, *, parent: Path | None = None, timeout_s: float = CANARY_TIMEOUT_S
) -> AdapterCanaryResult:
    """Observe typed clarification for a deliberately unspecified operator decision (8.2)."""
    from relay.adapters.parsing import claude_schema, drop_nulls, strict_schema
    from relay.core.specs import ResultSpec, schema_for

    base = Path(tempfile.mkdtemp(prefix="relay-input-canary-", dir=parent))
    try:
        work = base / "work"
        work.mkdir()
        subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
        schema = claude_schema() if spec.adapter == "claude" else schema_for(ResultSpec)
        schema.pop("$schema", None)
        if spec.adapter == "codex":
            schema = strict_schema(schema)
        prompt = (
            "Task id n1: Set the default behavior to the operator's preferred choice. "
            "The operator has not specified that choice; the repository cannot resolve it. "
            "Do not use tools or modify files. If a decision cannot be resolved from the "
            "repository, stop and return status needs_input with typed open_questions "
            "instead of guessing. Return a result spec matching the supplied schema."
        )
        args, stdin, output_file = _canary_invocation(spec, work, prompt=prompt, schema=schema)
        proc, failure = _run_canary_process(
            spec,
            "needs_input",
            args,
            cwd=work,
            env=dict(os.environ),
            stdin=stdin,
            timeout_s=timeout_s,
        )
        if failure is not None:
            return failure
        assert proc is not None
        payload = _extract_canary_payload(spec, proc, output_file)
        candidate = payload.native
        if candidate is None and payload.text is not None:
            candidate = _try_json(payload.text)
        try:
            result = ResultSpec.model_validate(drop_nulls(candidate))
        except ValueError:
            return AdapterCanaryResult(
                spec.adapter, "needs_input", "failed", "no valid typed clarification result", {}
            )
        passed = (
            proc.exit_code == 0 and result.status == "needs_input" and bool(result.open_questions)
        )
        return AdapterCanaryResult(
            spec.adapter,
            "needs_input",
            "passed" if passed else "failed",
            "typed clarification observed" if passed else "agent did not request clarification",
            {"status": result.status, "question_count": len(result.open_questions)},
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)
