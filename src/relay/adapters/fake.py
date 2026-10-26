"""`fake` adapter: scriptable stand-in for a real agent CLI (design section 7.1, tests only)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from relay.core.specs import ResultSpec, TaskSpec
from relay.core.specs.common import PermissionLevel

from .base import Invocation, Probe, ProcessResult, RunContext, WorkerHandle, task_prompt
from .fake_scenario import Scenario
from .parsing import (
    ABSENT,
    ResultInvalid,
    node_file_source,
    parse_sources,
    stdout_block_source,
    validate_result,
)

__all__ = ["FakeAdapter", "ResultInvalid"]

ScenarioFn = Callable[[TaskSpec, RunContext], Scenario]
Scripted = Scenario | Sequence[Scenario]
ScenarioSource = Scripted | Mapping[str, Scripted] | ScenarioFn


def _as_list(scripted: Scripted) -> list[Scenario]:
    return [scripted] if isinstance(scripted, Scenario) else list(scripted)


class FakeAdapter:
    id = "fake"

    def __init__(
        self,
        scenarios: ScenarioSource | None = None,
        *,
        available: bool = True,
        levels: tuple[PermissionLevel, ...] = ("full", "workspace", "readonly"),
        version: str = "fake-1.0",
    ) -> None:
        self._table: dict[str, list[Scenario]] = {}
        self._fn: ScenarioFn | None = None
        if callable(scenarios) and not isinstance(scenarios, Scenario):
            self._fn = scenarios
        elif isinstance(scenarios, Mapping):
            for node_id, scripted in scenarios.items():
                self.script(node_id, *_as_list(scripted))
        elif scenarios is not None:
            self.script("*", *_as_list(scenarios))
        self._available = available
        self._levels = levels
        self._version = version
        self.invocations: list[Invocation] = []
        self.cancelled: list[WorkerHandle] = []

    # --- scripting ------------------------------------------------------------------------

    def script(self, node_id: str, *scenarios: Scenario) -> FakeAdapter:
        """Set the per-attempt scenarios of one node (`"*"` is the default for other nodes)."""
        self._table[node_id] = list(scenarios)
        return self

    def scenario_for(self, task: TaskSpec, ctx: RunContext) -> Scenario:
        if self._fn is not None:
            return self._fn(task, ctx)
        sequence = self._table.get(ctx.node_id) or self._table.get("*") or []
        if not sequence:
            return Scenario()
        return sequence[min(ctx.attempt, len(sequence)) - 1]  # the last scenario repeats

    # --- AgentAdapter ---------------------------------------------------------------------

    def reuse_fingerprint(self, task: TaskSpec) -> str:
        return self._version

    async def probe(self) -> Probe:
        if not self._available:
            return Probe(available=False, notes=("fake adapter marked unavailable",))
        return Probe(available=True, version=self._version, levels=self._levels)

    def build_invocation(self, task: TaskSpec, ctx: RunContext) -> Invocation:
        scenario = self.scenario_for(task, ctx)
        ctx.node_dir.mkdir(parents=True, exist_ok=True)
        scenario_file = ctx.node_dir / "fake-scenario.json"
        scenario_file.write_text(
            json.dumps(
                {
                    "scenario": scenario.model_dump(mode="json"),
                    "runtime": {
                        "node_id": ctx.node_id,
                        "node_dir": str(ctx.node_dir),
                        "attempt": ctx.attempt,
                        "resume_session": ctx.resume_session,
                    },
                }
            ),
            encoding="utf-8",
        )
        invocation = Invocation(
            cmd=sys.executable,
            args=("-I", "-m", "relay.adapters.fake_cli", str(scenario_file)),
            cwd=ctx.worktree,
            stdin=task_prompt(task) + "\n",
        )
        self.invocations.append(invocation)
        return invocation

    async def parse_result(
        self, proc: ProcessResult, task: TaskSpec, ctx: RunContext
    ) -> ResultSpec:
        """Parsing order of design section 7.3: native, node-dir file, stdout block, synthesized."""
        return parse_sources(
            (
                lambda: self._native(proc),
                lambda: node_file_source(ctx),
                lambda: stdout_block_source(proc.stdout),
                lambda: self._synthesized(proc, ctx),
            ),
            finalize=lambda candidate: validate_result(
                candidate, node_id=ctx.node_id, agent="fake"
            ),
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        self.cancelled.append(handle)
        handle.kill_tree()

    # --- parsing helpers ------------------------------------------------------------------

    @staticmethod
    def _native(proc: ProcessResult) -> object:
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    return ABSENT
                if not isinstance(payload, dict):
                    return ABSENT
                result: Any = cast("dict[str, Any]", payload).get("result")
                return cast("dict[str, Any]", result) if isinstance(result, dict) else ABSENT
        return ABSENT

    @staticmethod
    def _synthesized(proc: ProcessResult, ctx: RunContext) -> dict[str, Any]:
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        ok = proc.exit_code == 0 and not proc.timed_out
        return {
            "id": ctx.node_id,
            "status": "done" if ok else "failed",
            "agent": "fake",
            "summary": lines[-1] if lines else "",
        }
