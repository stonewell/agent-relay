"""Public foreground graph CLI and deterministic templates."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness import init_repo
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.cli.commands import graph as commands
from relay.cli.main import main
from relay.core import graph as core
from relay.core.planner import TEMPLATES, plan
from relay.core.registry import Config, LoadedConfig, snapshot, validate_merged
from relay.core.specs import Graph
from relay.core.store import RunStore


@pytest.mark.parametrize("template", TEMPLATES)
def test_templates_validate(template: str) -> None:
    generated = plan("Add a feature", template=template, checks=("echo ok",))
    validated = Graph.model_validate(generated.model_dump(mode="json", by_alias=True))
    assert [node.id for node in validated.ordered_nodes()] == [
        "research",
        "implement",
        "tests",
        "review",
    ]


def test_plan_writes_graph_and_refuses_overwrite(tmp_path: Path, capsys: Any) -> None:
    assert (
        main(["-C", str(tmp_path), "plan", "Implement something", "--check", "echo ok", "--json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["file"] == "graph.yaml"
    Graph.model_validate(yaml.safe_load((tmp_path / "graph.yaml").read_text()))
    assert main(["-C", str(tmp_path), "plan", "Replace", "--json"]) == 2
    assert "already exists" in json.loads(capsys.readouterr().err)["error"]


def test_graph_run_resume_and_result(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    import os

    for key in list(os.environ):
        if key.endswith(("_KEY", "_TOKEN")):
            monkeypatch.delenv(key)
    repo = init_repo(tmp_path / "repo")
    spec = Graph.model_validate(
        {
            "verify": "none",
            "verification_skip_reason": "Test CLI plumbing",
            "nodes": [
                {"category": "implement", "goal": "Work", **node}
                for node in [
                    {"id": "a", "routing": {"agent": "claude"}},
                    {"id": "b", "depends_on": ["a"], "routing": {"agent": "codex"}},
                ]
            ],
        }
    )
    # Keep the intake file outside the target repository's pinned clean snapshot.
    target = tmp_path / "graph.yaml"
    target.write_text(
        yaml.safe_dump(spec.model_dump(mode="json", by_alias=True, exclude_unset=True))
    )
    cfg = validate_merged(
        {
            "version": 1,
            "agents": {
                name: {"cmd": "fake", "models": {"balanced": "default"}}
                for name in ("claude", "codex")
            },
        }
    )
    text, digest = snapshot(cfg)

    def load(_repo: Path) -> LoadedConfig:
        return LoadedConfig(cfg, (), text, digest)

    monkeypatch.setattr(commands, "load_config", load)
    adapter = FakeAdapter(Scenario(write={"out.py": "ok"}))

    def adapters(_config: Config) -> dict[str, FakeAdapter]:
        return {"claude": adapter, "codex": adapter}

    monkeypatch.setattr(core, "_default_adapters", adapters)
    assert main(["-C", str(repo), "graph", "run", str(target), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    run_id = result["run_id"]
    assert main(["-C", str(repo), "resume", run_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "done"
    assert main(["-C", str(repo), "result", run_id, "a", "summary", "--json"]) == 0
    assert "fake agent finished" in capsys.readouterr().out
    assert RunStore(repo, run_id).read_run().state == "done"


def test_reply_to_waiting_node_in_failed_graph(tmp_path: Path, capsys: Any) -> None:
    from relay.core.answers import read_answers
    from relay.core.runlock import RunLock

    repo = init_repo(tmp_path / "repo")
    spec = Graph.model_validate(
        {
            "verify": "none",
            "verification_skip_reason": "Exercise independent failure",
            "nodes": [
                {"id": key, "category": "implement", "goal": "Work", "routing": {"agent": "fake"}}
                for key in ("a", "b")
            ],
        }
    )
    cfg = validate_merged(
        {
            "version": 1,
            "agents": {"fake": {"cmd": "fake", "models": {"balanced": "default"}}},
        }
    )
    adapter = FakeAdapter(
        {
            "a": Scenario(
                status="needs_input", questions=[{"id": "q1", "text": "Which behavior?"}]
            ),
            "b": Scenario(status="failed"),
        }
    )
    result = asyncio.run(core.run_graph(repo, spec, config=cfg, adapters={"fake": adapter}))
    assert result.state == "failed"
    assert result.run_id is not None
    capsys.readouterr()
    store = RunStore(repo, result.run_id)
    args = ["-C", str(repo), "reply", result.run_id, "a", "Simple behavior", "--yes", "--json"]
    with RunLock.acquire(repo, result.run_id):
        assert main(args) == 1
    assert "in progress" in json.loads(capsys.readouterr().err)["error"]
    assert not read_answers(store.node_dir("a"), "a").submissions
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["recorded"]
    assert read_answers(store.node_dir("a"), "a").submissions[0].answers == {
        "q1": "Simple behavior"
    }
    assert store.read_run().state == "failed"


@pytest.mark.parametrize(
    "target,state",
    [
        ("resolve-1234", "running"),
        ("a", "done"),
        ("a", "cancelled"),
        ("a", "blocked"),
        ("a", "failed"),
    ],
)
def test_cancel_rejects_targets_without_scheduler_consumer(
    tmp_path: Path, capsys: Any, target: str, state: str
) -> None:
    repo = init_repo(tmp_path / "repo")
    store = RunStore.create(repo, config_hash="test")
    store.write_text("graph.marker", "foreground\n")
    spec = Graph.model_validate(
        {
            "nodes": [{"id": "a", "category": "implement", "goal": "Work"}],
        }
    )
    store.add_node("a")
    if target != "a":
        store.add_node(target)
    store.update_run(lambda r: setattr(r, "graph", spec))
    store.update_run(lambda r: setattr(r.nodes[target], "state", state))
    before = store.run_json.read_bytes()
    assert main(["-C", str(repo), "cancel", store.run_id, target, "--yes", "--json"]) == 2
    assert "whole run" in json.loads(capsys.readouterr().err)["error"]
    assert store.run_json.read_bytes() == before
    assert not store.path("cancel-requests").exists()
