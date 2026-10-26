"""Failure policy survives separate retries; clarification is not a failed attempt."""

import asyncio
import io
from pathlib import Path
from typing import Any

import pytest

from harness import git, init_repo
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.core.events import read_events
from relay.core.registry import Config, validate_merged
from relay.core.run import run_foreground
from relay.core.specs import TaskSpec, load_spec
from relay.core.store import RunStore

RUN = "2026-10-26-abcd"


def config() -> Config:
    return validate_merged(
        {
            "version": 1,
            "defaults": {"fallback": ["claude", "codex", "gemini"]},
            "agents": {
                "claude": {"cmd": "fake", "models": {"fast": "small", "deep": "large"}},
                "codex": {"cmd": "fake", "models": {"balanced": "codex-model"}},
                "gemini": {"cmd": "fake", "models": {"balanced": "gemini-model"}},
            },
        }
    )


def task(limit: int) -> TaskSpec:
    result = TaskSpec(
        run_id=RUN,
        id="a",
        category="implement",
        goal="Implement a change",
        verify="none",
        verification_skip_reason="Exercise attempt routing",
    )
    result.budget.max_attempts = limit
    return result


def routes(store: RunStore) -> list[Any]:
    return [
        event.data["decision"]
        for event in read_events(store.events_path)
        if event.type == "node.routed"
    ]


@pytest.mark.parametrize("split", [False, True])
def test_escalation_and_excluded_fallback_history_survive_retry(
    tmp_path: Path, split: bool
) -> None:
    repo = init_repo(tmp_path / "repo")
    cfg = config()
    adapters = {
        name: FakeAdapter(
            lambda task, ctx: Scenario(
                status="failed" if ctx.attempt < 4 else "done",
                write={f"attempt-{ctx.attempt}.txt": "kept"},
            )
        )
        for name in cfg.agents
    }
    initial = asyncio.run(
        run_foreground(
            repo,
            task(1 if split else 4),
            config=cfg,
            adapters=adapters,
            run_id=RUN,
        )
    )
    store = RunStore(repo, RUN)
    if split:
        for limit in (2, 3, 4):
            assert initial.exit_code == 1, initial.to_json()
            resumed = load_spec(TaskSpec, store.node_dir("a") / "task.spec.yaml")
            resumed.budget.max_attempts = limit
            initial = asyncio.run(
                run_foreground(
                    repo,
                    resumed,
                    config=cfg,
                    adapters=adapters,
                    resume_run=RUN,
                    retry_failed=True,
                )
            )
    assert initial.exit_code == 0, initial.to_json()
    decisions = routes(store)
    assert [(r["agent"], r["model"], r["event"]) for r in decisions] == [
        ("claude", "small", "routed"),
        ("claude", "large", "escalated"),
        ("codex", "codex-model", "fallback"),
        ("gemini", "gemini-model", "fallback"),
    ]
    assert [r["attempt"] for r in decisions] == [1, 2, 3, 4]
    assert decisions[1]["from"]["model"] == "small"
    assert store.read_run().nodes["a"].attempts == 4
    for number in range(1, 5):
        assert git(repo, "show", f"{initial.integration_commit}:attempt-{number}.txt") == "kept"


def test_interactive_clarification_preserves_resolved_model_then_failure_escalates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("builtins.input", lambda: "Proceed")
    repo = init_repo(tmp_path / "repo")
    cfg = config()
    cfg.agents["claude"].models = {"fast": "small", "balanced": "medium", "deep": "large"}
    cfg.agents["claude"].model_routing = type(cfg.agents["claude"].model_routing).model_validate(
        {
            "rules": [{"id": "files-rule", "when": {"files": ">=1"}, "use": {"model": "balanced"}}],
        }
    )
    spec = task(3)
    spec.context.files = ["README.md"]
    seen: list[tuple[str | None, str | None]] = []

    def scenario(task: TaskSpec, ctx: Any) -> Scenario:
        seen.append((task.routing.agent, task.routing.model))
        if ctx.attempt == 1:
            return Scenario(status="needs_input", questions=[{"id": "q1", "text": "Proceed?"}])
        return Scenario(status="failed" if ctx.attempt == 2 else "done")

    fallback = FakeAdapter()
    result = asyncio.run(
        run_foreground(
            repo,
            spec,
            config=cfg,
            run_id=RUN,
            adapters={"claude": FakeAdapter(scenario), "codex": fallback, "gemini": fallback},
            out=Terminal(),
        )
    )
    assert result.exit_code == 0, result.to_json()
    assert seen == [("claude", "medium"), ("claude", "medium"), ("claude", "large")]
    assert not fallback.invocations
    decisions = routes(RunStore(repo, RUN))
    assert [r["event"] for r in decisions] == ["routed", "routed", "escalated"]
    assert decisions[1].get("from") is None
    assert decisions[2]["from"]["model"] == "medium"


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("tier,model", [("fast", "small"), ("deep", "large")])
def test_model_override_logs_actual_model_without_false_escalation(
    tmp_path: Path, split: bool, tier: str, model: str
) -> None:
    repo = init_repo(tmp_path / "repo")
    spec = task(1 if split else 2)
    spec.routing.agent = "claude"
    adapter = FakeAdapter([Scenario(status="failed"), Scenario()])
    result = asyncio.run(
        run_foreground(
            repo,
            spec,
            config=config(),
            run_id=RUN,
            model_tier=tier,
            adapters={"claude": adapter},
        )
    )
    if split:
        assert result.exit_code == 1
        resumed = load_spec(TaskSpec, RunStore(repo, RUN).node_dir("a") / "task.spec.yaml")
        resumed.budget.max_attempts = 2
        result = asyncio.run(
            run_foreground(
                repo,
                resumed,
                config=config(),
                resume_run=RUN,
                retry_failed=True,
                adapters={"claude": adapter},
            )
        )
    assert result.exit_code == 0, result.to_json()
    decisions = routes(RunStore(repo, RUN))
    assert [r["model"] for r in decisions] == [model, model]
    assert [r["event"] for r in decisions] == ["routed", "routed"]


def test_interrupted_fallback_restores_last_admitted_route(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    cfg = config()
    cfg.agents["claude"].models = {"fast": "small"}
    cfg.agents["codex"].permissions = "readonly"
    original = FakeAdapter(Scenario(status="failed"))
    permissions: list[tuple[str | None, str]] = []

    def interrupt(task: TaskSpec, ctx: Any) -> Scenario:
        permissions.append((task.permissions, ctx.permission))
        raise KeyboardInterrupt

    result = asyncio.run(
        run_foreground(
            repo,
            task(3),
            config=cfg,
            run_id=RUN,
            adapters={"claude": original, "codex": FakeAdapter(interrupt)},
        )
    )
    assert result.exit_code == 4, result.to_json()
    store = RunStore(repo, RUN)
    resumed = load_spec(TaskSpec, store.node_dir("a") / "task.spec.yaml")
    # Older snapshots retained the first route even after admitting a fallback.
    resumed.routing.agent, resumed.routing.model = "claude", "small"

    def complete(task: TaskSpec, ctx: Any) -> Scenario:
        permissions.append((task.permissions, ctx.permission))
        return Scenario()

    replacement = FakeAdapter(complete)
    result = asyncio.run(
        run_foreground(
            repo,
            resumed,
            config=cfg,
            resume_run=RUN,
            adapters={"claude": original, "codex": replacement},
        )
    )
    assert result.exit_code == 0, result.to_json()
    assert len(original.invocations) == len(replacement.invocations) == 1
    decisions = routes(store)
    assert [(r["agent"], r["model"], r["event"]) for r in decisions] == [
        ("claude", "small", "routed"),
        ("codex", "codex-model", "fallback"),
        ("codex", "codex-model", "routed"),
    ]
    assert decisions[-1].get("from") is None
    assert store.read_run().nodes["a"].attempts == 3
    assert permissions == [("readonly", "readonly"), ("readonly", "readonly")]
    assert decisions[-1]["permissions"] == "readonly"


@pytest.mark.parametrize("split", [False, True])
def test_per_agent_cli_permission_ceiling_survives_fallback_and_retry(
    tmp_path: Path, split: bool
) -> None:
    repo = init_repo(tmp_path / "repo")
    cfg = config()
    cfg.agents["claude"].models = {"fast": "small"}
    permissions: list[tuple[str | None, str]] = []

    def complete(task: TaskSpec, ctx: Any) -> Scenario:
        permissions.append((task.permissions, ctx.permission))
        return Scenario()

    adapters = {"claude": FakeAdapter(Scenario(status="failed")), "codex": FakeAdapter(complete)}
    result = asyncio.run(
        run_foreground(
            repo,
            task(1 if split else 2),
            config=cfg,
            run_id=RUN,
            adapters=adapters,
            agent_permissions={"codex": "readonly"},
        )
    )
    store = RunStore(repo, RUN)
    assert cfg.agents["codex"].permissions is None
    saved_config = Config.model_validate_json(store.path("config.snapshot.json").read_text())
    assert saved_config.agents["codex"].permissions == "readonly"
    if split:
        assert result.exit_code == 1
        resumed = load_spec(TaskSpec, store.node_dir("a") / "task.spec.yaml")
        resumed.budget.max_attempts = 2
        result = asyncio.run(
            run_foreground(
                repo,
                resumed,
                config=saved_config,
                resume_run=RUN,
                retry_failed=True,
                adapters=adapters,
            )
        )
    assert result.exit_code == 0, result.to_json()
    assert permissions == [("readonly", "readonly")]
    assert routes(store)[-1]["permissions"] == "readonly"


def test_failed_unpinned_agent_becoming_unavailable_can_fallback_on_retry(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    cfg = config()
    first = asyncio.run(
        run_foreground(
            repo,
            task(1),
            config=cfg,
            run_id=RUN,
            adapters={"claude": FakeAdapter(Scenario(status="failed"))},
        )
    )
    assert first.exit_code == 1
    store = RunStore(repo, RUN)
    resumed = load_spec(TaskSpec, store.node_dir("a") / "task.spec.yaml")
    resumed.budget.max_attempts = 2
    replacement = FakeAdapter()
    result = asyncio.run(
        run_foreground(
            repo,
            resumed,
            config=cfg,
            resume_run=RUN,
            retry_failed=True,
            adapters={"claude": FakeAdapter(available=False), "codex": replacement},
        )
    )
    assert result.exit_code == 0, result.to_json()
    assert len(replacement.invocations) == 1
    assert routes(store)[-1]["agent"] == "codex"
    assert routes(store)[-1]["event"] == "fallback"


@pytest.mark.parametrize("split", [False, True])
def test_logical_model_override_survives_cross_agent_retry(tmp_path: Path, split: bool) -> None:
    repo = init_repo(tmp_path / "repo")
    cfg = config()
    cfg.agents["codex"].models = {"fast": "codex-small", "deep": "codex-large"}
    seen: list[str | None] = []

    def complete(task: TaskSpec, ctx: Any) -> Scenario:
        seen.append(task.routing.model)
        return Scenario()

    adapters = {"claude": FakeAdapter(Scenario(status="failed")), "codex": FakeAdapter(complete)}
    result = asyncio.run(
        run_foreground(
            repo,
            task(1 if split else 2),
            config=cfg,
            run_id=RUN,
            adapters=adapters,
            model_tier="deep",
        )
    )
    store = RunStore(repo, RUN)
    if split:
        assert result.exit_code == 1
        resumed = load_spec(TaskSpec, store.node_dir("a") / "task.spec.yaml")
        assert resumed.routing.model == "large"
        resumed.budget.max_attempts = 2
        result = asyncio.run(
            run_foreground(
                repo,
                resumed,
                config=cfg,
                resume_run=RUN,
                retry_failed=True,
                adapters=adapters,
            )
        )
    assert result.exit_code == 0, result.to_json()
    assert seen == ["codex-large"]
    decisions = routes(store)
    assert [(r["agent"], r["model"], r["event"]) for r in decisions] == [
        ("claude", "large", "routed"),
        ("codex", "codex-large", "fallback"),
    ]
    checkpoint = store.read_run().graph
    assert checkpoint is not None
    assert checkpoint.nodes[0].routing.model == "deep"
    assert (
        load_spec(TaskSpec, store.node_dir("a") / "task.spec.yaml").routing.model == "codex-large"
    )
