"""Authorized single-node CLI intake and delivery (task 1.31)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness import command_line, git, init_repo
from relay.adapters.base import AgentAdapter
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.cli.commands import run as run_cli
from relay.cli.exitcodes import Exit
from relay.cli.main import main
from relay.core import run as core_run
from relay.core.registry import Config, LoadedConfig, snapshot, validate_merged
from relay.core.specs import ResultSpec, TaskSpec
from relay.core.specs.task import RunCheck, ScopeCheck
from relay.core.store import RunStore


@pytest.fixture
def running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FakeAdapter]:
    repo = init_repo(tmp_path / "repo")
    adapter = FakeAdapter(Scenario(write={"feature.txt": "feature\n"}))
    config = validate_merged(
        {
            "version": 1,
            "defaults": {"permissions": "full", "timeout_s": 10, "fallback": ["fake"]},
            "agents": {
                "fake": {"cmd": "fake", "categories": ["implement"], "models": {"fast": "fake"}}
            },
        }
    )
    text, digest = snapshot(config)
    loaded = LoadedConfig(config=config, sources=(), snapshot_json=text, config_hash=digest)

    def load(project_root: Path | None = None) -> LoadedConfig:
        return loaded

    def adapters(config: Config) -> dict[str, AgentAdapter]:
        return {"fake": adapter}

    monkeypatch.setattr(run_cli, "load_config", load)
    monkeypatch.setattr(core_run, "_default_adapters", adapters)
    return repo, adapter


def check(code: str = "assert True") -> str:
    return command_line([sys.executable, "-c", code])


def write_task(tmp_path: Path, **changes: Any) -> Path:
    data: dict[str, Any] = {
        "spec_version": 1,
        "run_id": "auto",
        "id": "n1",
        "category": "implement",
        "goal": "Add a feature",
        "commands": {"test": check()},
    }
    data.update(changes)
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_request_check_runs_and_applies(
    running: tuple[Path, FakeAdapter], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = running
    command = check(
        "from pathlib import Path; assert Path('feature.txt').read_text() == 'feature\\n'"
    )
    assert main(["-C", str(repo), "run", "Add a feature", "--check", command, "--json"]) == Exit.OK
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert "permissions" in output.err
    store = RunStore.open(repo, payload["run_id"])
    record = store.read_run()
    assert record.graph is not None
    assert record.graph.integration_acceptance == [RunCheck(run=command)]
    result = store.read_spec(f"{record.nodes['n1'].dir}/result.spec.yaml", ResultSpec)
    assert result.verification is not None
    assert result.verification.acceptance == "passed"
    assert not (repo / "feature.txt").exists()

    assert main(["-C", str(repo), "apply", store.run_id, "--yes", "--json"]) == Exit.OK
    delivered = json.loads(capsys.readouterr().out)
    assert delivered["verification"]["outcome"] == "passed"
    assert (repo / "feature.txt").read_text() == "feature\n"


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_task_file_resolves_against_repo_and_overrides_are_validated(
    running: tuple[Path, FakeAdapter],
    tmp_path: Path,
    format: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _ = running
    path = write_task(
        tmp_path,
        category="bugfix",
        acceptance=[{"run": check("raise SystemExit(1)")}, {"diff_scope": ["feature.txt"]}],
        commands={"build": check(), "test": check("raise SystemExit(1)")},
        permissions="workspace",
        budget={"max_attempts": 2, "timeout_s": 8},
    )
    if format == "json":
        path.write_text(json.dumps(yaml.safe_load(path.read_text())), encoding="utf-8")
    status = main(
        [
            "-C",
            str(repo),
            "run",
            "--task",
            "../task.yaml",
            "--category",
            "implement",
            "--agent",
            "fake",
            "--check",
            check(),
            "--test",
            check(),
            "--max-attempts",
            "3",
            "--timeout",
            "4",
            "--json",
        ]
    )
    assert status == Exit.OK
    store = RunStore.open(repo, json.loads(capsys.readouterr().out)["run_id"])
    task = store.read_spec(f"{store.read_run().nodes['n1'].dir}/task.spec.yaml", TaskSpec)
    assert task.category == "implement"
    assert task.routing.agent == "fake"
    assert task.permissions == "workspace"
    assert task.acceptance == [ScopeCheck(diff_scope=["feature.txt"]), RunCheck(run=check())]
    assert task.commands is not None
    assert task.commands.build == check()
    assert task.commands.test == check()
    assert task.budget.max_attempts == 3
    assert task.budget.timeout_s == 4


def test_task_file_drives_same_agent_retry(
    running: tuple[Path, FakeAdapter], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, adapter = running
    adapter.script("*", Scenario(status="failed"), Scenario(write={"feature.txt": "feature\n"}))
    write_task(tmp_path, budget={"max_attempts": 2})
    assert main(["-C", str(repo), "run", "--task", "../task.yaml", "--json"]) == Exit.OK
    store = RunStore.open(repo, json.loads(capsys.readouterr().out)["run_id"])
    assert store.read_run().nodes["n1"].attempts == 2
    assert len(adapter.invocations) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"depends_on": ["upstream"]},
        {"context": {"from": [{"node": "upstream", "field": "summary"}]}},
        {"repo": {"root": "another-repo"}},
        {"repo": {"input_commit": "a" * 40}},
        {"budget": {"max_cost_usd": 1}},
        {"output": {"result": "custom.yaml"}},
        {"output": {"artifacts_dir": "custom/"}},
    ],
)
def test_unavailable_inputs_fail_before_dispatch(
    running: tuple[Path, FakeAdapter],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, Any],
) -> None:
    repo, adapter = running
    write_task(tmp_path, **changes)
    assert main(["-C", str(repo), "run", "--task", "../task.yaml", "--json"]) == Exit.INVALID
    assert "error" in json.loads(capsys.readouterr().err)
    assert not adapter.invocations
    assert not (repo / ".agents").exists()


@pytest.mark.parametrize("node_id", ["base", "integration"])
def test_reserved_node_ids_are_rejected_before_dispatch(
    running: tuple[Path, FakeAdapter],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    node_id: str,
) -> None:
    repo, adapter = running
    write_task(tmp_path, id=node_id)
    assert main(["-C", str(repo), "run", "--task", "../task.yaml", "--json"]) == Exit.INVALID
    assert "error" in json.loads(capsys.readouterr().out)
    assert not adapter.invocations
    assert not (repo / ".agents").exists()


@pytest.mark.parametrize("flag", ["--max-attempts", "--timeout"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "invalid"])
def test_invalid_limits_are_rejected(
    running: tuple[Path, FakeAdapter], flag: str, value: str
) -> None:
    repo, adapter = running
    with pytest.raises(SystemExit) as failure:
        main(["-C", str(repo), "run", "Add a feature", flag, value])
    assert failure.value.code == 2
    assert not adapter.invocations
    assert not (repo / ".agents").exists()


@pytest.mark.parametrize("contents", ["goal: [", "goal: first\ngoal: second\n"])
def test_malformed_or_duplicate_yaml_is_a_clean_input_error(
    running: tuple[Path, FakeAdapter],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    contents: str,
) -> None:
    repo, adapter = running
    (tmp_path / "task.yaml").write_text(contents, encoding="utf-8")
    assert main(["-C", str(repo), "run", "--task", "../task.yaml", "--json"]) == Exit.INVALID
    assert "error" in json.loads(capsys.readouterr().err)
    assert not adapter.invocations


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["request", "--task", "missing.yaml"],
        ["--task", "missing.yaml"],
        ["request", "--verify", "none"],
        ["request", "--check", "   "],
    ],
)
def test_invalid_intake_never_starts_an_agent(
    running: tuple[Path, FakeAdapter], arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, adapter = running
    assert main(["-C", str(repo), "run", *arguments, "--json"]) == Exit.INVALID
    output = capsys.readouterr()
    json.loads(output.err if not output.out else output.out)
    assert not adapter.invocations
    assert not (repo / ".agents").exists()


def test_explicit_skip_is_preserved_through_apply(
    running: tuple[Path, FakeAdapter], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = running
    assert (
        main(
            [
                "-C",
                str(repo),
                "run",
                "Add a feature",
                "--verify",
                "none",
                "--verification-skip-reason",
                "manual review",
                "--json",
            ]
        )
        == Exit.OK
    )
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert main(["-C", str(repo), "apply", run_id, "--yes", "--json"]) == Exit.OK
    assert json.loads(capsys.readouterr().out)["verification"]["outcome"] == "skipped"


def test_timeout_retries_are_bounded_by_the_total_attempt_limit(
    running: tuple[Path, FakeAdapter], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, adapter = running
    adapter.script("*", Scenario(hang=True))
    assert (
        main(
            [
                "-C",
                str(repo),
                "run",
                "Add a feature",
                "--check",
                check(),
                "--timeout",
                "1",
                "--max-attempts",
                "3",
                "--json",
            ]
        )
        == Exit.FAILURE
    )
    payload = json.loads(capsys.readouterr().out)
    assert "timed out" in payload["error"]
    assert len(adapter.invocations) == 3
    record = RunStore(repo, payload["run_id"]).read_run()
    assert record.nodes["n1"].attempts == 3


def test_apply_rechecks_the_authorized_policy_on_a_changed_target(
    running: tuple[Path, FakeAdapter], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = running
    (repo / "mode.txt").write_text("good")
    git(repo, "add", "mode.txt")
    git(repo, "commit", "-m", "prepare acceptance input")
    command = check("from pathlib import Path; assert Path('mode.txt').read_text() == 'good'")
    assert main(["-C", str(repo), "run", "Add a feature", "--check", command, "--json"]) == Exit.OK
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    (repo / "mode.txt").write_text("bad")
    git(repo, "add", "mode.txt")
    git(repo, "commit", "-m", "change acceptance input")
    target = git(repo, "rev-parse", "HEAD")
    assert main(["-C", str(repo), "apply", run_id, "--yes", "--json"]) == Exit.VERIFICATION_FAILED
    assert git(repo, "rev-parse", "HEAD") == target
    assert not (repo / "feature.txt").exists()
