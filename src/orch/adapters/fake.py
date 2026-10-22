"""`fake` adapter: scriptable stand-in for a real agent CLI (design section 7.1, tests only)."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from pydantic import ValidationError

from orch.core.specs import ResultSpec, TaskSpec, parse_spec
from orch.core.specs.common import PermissionLevel

from .base import Invocation, Probe, ProcessResult, RunContext, WorkerHandle
from .fake_scenario import Scenario

ScenarioFn = Callable[[TaskSpec, RunContext], Scenario]
Scripted = Scenario | Sequence[Scenario]
ScenarioSource = Scripted | Mapping[str, Scripted] | ScenarioFn

_BLOCK = re.compile(r"```(?:json|yaml)\s*\n(.*?)\n```", re.DOTALL)


def _as_list(scripted: Scripted) -> list[Scenario]:
    return [scripted] if isinstance(scripted, Scenario) else list(scripted)


class ResultInvalid(ValueError):
    """The agent produced a result that does not validate against the spec schema."""


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
            args=("-I", "-m", "orch.adapters.fake_cli", str(scenario_file)),
            cwd=ctx.worktree,
            stdin=task.goal + "\n",
        )
        self.invocations.append(invocation)
        return invocation

    async def parse_result(
        self, proc: ProcessResult, task: TaskSpec, ctx: RunContext
    ) -> ResultSpec:
        """Parsing order of design section 7.3: native, node-dir file, stdout block, synthesized."""
        for candidate in (
            self._native(proc),
            self._node_file(ctx),
            self._stdout_block(proc),
        ):
            if candidate is not None:
                try:
                    return ResultSpec.model_validate(candidate)
                except ValidationError as exc:
                    raise ResultInvalid(str(exc)) from exc
        return self._synthesized(proc, ctx)

    async def cancel(self, handle: WorkerHandle) -> None:
        self.cancelled.append(handle)
        handle.kill_tree()

    # --- parsing helpers ------------------------------------------------------------------

    @staticmethod
    def _native(proc: ProcessResult) -> dict[str, Any] | None:
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    return None
                if not isinstance(payload, dict):
                    return None
                result: Any = cast("dict[str, Any]", payload).get("result")
                return cast("dict[str, Any]", result) if isinstance(result, dict) else None
        return None

    @staticmethod
    def _node_file(ctx: RunContext) -> dict[str, Any] | None:
        path = ctx.node_dir / "result.spec.yaml"
        if not path.is_file():
            return None
        spec = parse_spec(ResultSpec, path.read_text(encoding="utf-8"))
        return spec.model_dump(mode="json")

    @staticmethod
    def _stdout_block(proc: ProcessResult) -> dict[str, Any] | None:
        match = _BLOCK.search(proc.stdout)
        if match is None:
            return None
        spec = parse_spec(ResultSpec, match.group(1))
        return spec.model_dump(mode="json")

    @staticmethod
    def _synthesized(proc: ProcessResult, ctx: RunContext) -> ResultSpec:
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        ok = proc.exit_code == 0 and not proc.timed_out
        return ResultSpec(
            id=ctx.node_id,
            status="done" if ok else "failed",
            agent="fake",
            summary=lines[-1] if lines else "",
        )
