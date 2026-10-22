"""`orch doctor` framework: check registry, runner and report (design section 14)."""

from __future__ import annotations

import asyncio
import json
import platform
import shutil
import subprocess
import sys
import traceback
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import registry as registry_module
from .gitexclude import check_exclude_block
from .registry import ConfigError, LoadedConfig, load_config

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
            remedy="run `git init`; orch needs a git repository in phases 1 to 3",
        )
    if state == "malformed":
        return CheckResult(
            "fail", "exclude block is malformed", remedy=f"{detail}; repair it by hand"
        )
    return CheckResult(
        "warn",
        f"exclude block is {state}: {detail}",
        remedy="the next `orch run` adds it; until then `git status` may list .agents/runs",
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


def default_registry(ctx: DoctorContext | None = None) -> CheckRegistry:
    """The built-in checks. Agent checks are added from the configuration if it loads."""
    registry = CheckRegistry()
    registry.register(Check("python", "Python version", check_python))
    registry.register(Check("git", "git is available", check_git))
    registry.register(Check("config", "agents.yaml is valid", check_config))
    registry.register(
        Check("git.exclude", "runtime state is excluded", check_git_exclude, requires=("git",))
    )
    if ctx is not None:
        try:
            loaded = load_config(ctx.repo_root, user_config=ctx.user_config)
        except ConfigError:
            return registry  # the `config` check reports the problem
        for name, agent in loaded.config.agents.items():
            if agent.enabled:
                registry.register(agent_cli_check(name, agent.cmd))
    return registry
