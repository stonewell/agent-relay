"""Offline production-adapter reuse and conservative admission of replacement attempts."""

import asyncio
import json
from pathlib import Path

import pytest

from harness import git, init_repo
from relay.adapters.base import Invocation, Probe, ProcessResult
from relay.adapters.claude import ClaudeAdapter
from relay.adapters.codex import CodexAdapter
from relay.core import run as attempts
from relay.core.graph import run_graph
from relay.core.publish import node_ref
from relay.core.registry import Agent, validate_merged
from relay.core.specs import Graph, TaskSpec
from relay.core.store import RunStore


def test_claude_fingerprint_tracks_effective_invocation_inputs() -> None:
    task = TaskSpec(
        run_id="2026-10-26-abcd", id="a", category="implement", goal="Work", permissions="full"
    )
    task.routing.model = "claude-explicit-model"
    parent = {"ANTHROPIC_API_KEY": "parent-secret", "HTTPS_PROXY": "http://user:password@proxy"}
    config = Agent(cmd="claude", env={"ANTHROPIC_API_KEY": "overridden-secret"})
    adapter = ClaudeAdapter(config, parent_env=parent)
    baseline = adapter.reuse_fingerprint(task)
    assert baseline is not None and len(baseline) == 64
    assert "secret" not in baseline and "password" not in baseline
    # Auth extras win over agent.env in the actual invocation as well as the fingerprint.
    config.env["ANTHROPIC_API_KEY"] = "different-overridden-secret"
    assert adapter.reuse_fingerprint(task) == baseline
    parent["ANTHROPIC_API_KEY"] = "new-effective-secret"
    assert adapter.reuse_fingerprint(task) != baseline
    parent["ANTHROPIC_API_KEY"] = "parent-secret"
    parent["HTTPS_PROXY"] = "http://different:credential@proxy"
    assert adapter.reuse_fingerprint(task) != baseline
    parent["HTTPS_PROXY"] = "http://user:password@proxy"
    task.permissions = "readonly"
    assert adapter.reuse_fingerprint(task) != baseline
    task.permissions = "full"
    task.routing.effort = "high"
    assert adapter.reuse_fingerprint(task) != baseline
    task.routing.effort = None
    assert (
        ClaudeAdapter(config, cmd="other-command", parent_env=parent).reuse_fingerprint(task)
        != baseline
    )
    assert ClaudeAdapter(Agent(cmd="claude"), parent_env={}).reuse_fingerprint(task) is None
    for model in (None, "default", "", " "):
        task.routing.model = model
        assert adapter.reuse_fingerprint(task) is None
    task.routing.model = "claude-explicit-model"
    config.env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    assert adapter.reuse_fingerprint(task) is None


@pytest.mark.parametrize("comparable", [True, False])
def test_production_reuse_and_retained_output_retry_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comparable: bool
) -> None:
    repo = init_repo(tmp_path / "repo")
    cfg = validate_merged(
        {
            "version": 1,
            "agents": {
                "claude": {
                    "cmd": "offline-claude",
                    "models": {"balanced": "claude-explicit-model"},
                },
                "codex": {"cmd": "offline-codex", "enabled": False},
            },
        }
    )
    spec = Graph.model_validate(
        {
            "verify": "none",
            "verification_skip_reason": "Offline production cache test",
            "nodes": [
                {"id": "a", "category": "implement", "goal": "Work", "routing": {"agent": "claude"}}
            ],
        }
    )
    calls: list[Invocation] = []

    async def probe(_adapter: object) -> Probe:
        return Probe(available=True, version="offline-1", levels=("full",))

    def execute(invocation: Invocation, *, timeout_s: float | None = None) -> ProcessResult:
        calls.append(invocation)
        assert invocation.cmd == "offline-claude"
        (invocation.cwd / "feature.txt").write_text("preserved", encoding="utf-8")
        return ProcessResult(exit_code=0, stdout=json.dumps({"type": "result", "result": "done"}))

    monkeypatch.setattr(ClaudeAdapter, "probe", probe)
    monkeypatch.setattr(CodexAdapter, "probe", probe)
    monkeypatch.setattr(attempts, "run_invocation", execute)
    parent = {"ANTHROPIC_API_KEY": "first-secret-value"} if comparable else {}

    def invoke(resume: str | None = None, limit: int | None = None):
        return asyncio.run(
            run_graph(
                repo,
                spec,
                config=cfg,
                resume_run=resume,
                max_attempts=limit,
                adapters={
                    "claude": ClaudeAdapter(cfg.agents["claude"], parent_env=parent),
                    "codex": CodexAdapter(cmd="offline-codex"),
                },
            )
        )

    initial = invoke()
    assert initial.exit_code == 0, initial.to_json()
    assert initial.run_id is not None
    store = RunStore(repo, initial.run_id)
    original_key = store.read_run().nodes["a"].input_key
    original_ref = git(repo, "rev-parse", node_ref(initial.run_id, "a"))
    original_result = (store.node_dir("a") / "result.spec.yaml").read_bytes()
    if comparable:
        reused = invoke(initial.run_id, 2)
        assert reused.exit_code == 0, reused.to_json()
        assert len(calls) == 1
        assert store.read_run().nodes["a"].attempts == 1
        assert store.read_run().nodes["a"].input_key == original_key
        assert store.read_run().graph is not None
        assert store.read_run().graph.nodes[0].budget.max_attempts is None  # pyright: ignore[reportOptionalMemberAccess]
        parent["ANTHROPIC_API_KEY"] = "changed-secret-value"
    blocked = invoke(initial.run_id)
    assert blocked.exit_code == 6, blocked.to_json()
    assert len(calls) == 1
    if not comparable:
        assert "execution_fingerprint_uncomparable" in str(blocked.nodes["a"].reason)
    assert git(repo, "rev-parse", node_ref(initial.run_id, "a")) == original_ref
    assert (store.node_dir("a") / "result.spec.yaml").read_bytes() == original_result
    assert store.read_run().nodes["a"].input_key == original_key
    replacement = invoke(initial.run_id, 2)
    assert replacement.exit_code == 0, replacement.to_json()
    assert len(calls) == 2
    assert store.read_run().nodes["a"].attempts == 2
    assert git(repo, "show", f"{replacement.integration_commit}:feature.txt") == "preserved"
    for path in store.run_dir.rglob("*"):
        if path.is_file() and path.name != "fake-scenario.json":
            content = path.read_bytes()
            assert b"first-secret-value" not in content
            assert b"changed-secret-value" not in content
