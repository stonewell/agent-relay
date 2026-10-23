"""The five foreground MVP acceptance scenarios (tasks 1.25 and 1.30)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness import command_line, init_repo
from relay.adapters.base import AgentAdapter
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.cli.commands import run as run_cli
from relay.cli.main import main
from relay.core import run as core_run
from relay.core.registry import Config, LoadedConfig, snapshot
from relay.core.store import RunStore
from test_run import config


@pytest.mark.parametrize("scenario", ["feature", "retry", "input", "verification", "dirty"])
def test_foreground_mvp_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
) -> None:
    repo = init_repo(tmp_path / "repo")
    success = Scenario(write={"feature.txt": "verified\n"})
    scripts = [success]
    if scenario == "retry":
        scripts = [Scenario(status="failed", write={"draft.txt": "preserved"}), success]
    elif scenario == "input":
        scripts = [
            Scenario(status="needs_input", questions=[{"id": "q1", "text": "Which behavior?"}]),
            success,
        ]
    adapter = FakeAdapter(scripts)
    conf = config()
    conf.agents["fake"].categories.append("bugfix")
    text, digest = snapshot(conf)

    def load(project_root: Path | None = None) -> LoadedConfig:
        return LoadedConfig(config=conf, sources=(), snapshot_json=text, config_hash=digest)

    def adapters(config: Config) -> dict[str, AgentAdapter]:
        return {"fake": adapter}

    monkeypatch.setattr(run_cli, "load_config", load)
    monkeypatch.setattr(core_run, "_default_adapters", adapters)
    code = (
        "raise SystemExit(1)"
        if scenario == "verification"
        else ("from pathlib import Path; assert Path('feature.txt').read_text() == 'verified\\n'")
    )
    exit_code = main(
        [
            "-C",
            str(repo),
            "run",
            "Fix a bug" if scenario == "retry" else "Implement a feature",
            "--agent",
            "fake",
            "--max-attempts",
            "2",
            "--check",
            command_line([sys.executable, "-c", code]),
            "--non-interactive",
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    run_id = result["run_id"]
    store = RunStore.open(repo, run_id)
    assert not (repo / "feature.txt").exists()
    if scenario == "verification":
        assert exit_code == 5
        assert store.read_run().final_commit is None
        assert main(["-C", str(repo), "apply", run_id, "--yes"]) != 0
        assert not (repo / "feature.txt").exists()
        return
    if scenario == "input":
        assert exit_code == 6
        assert (
            main(
                [
                    "-C",
                    str(repo),
                    "reply",
                    run_id,
                    "n1",
                    "Use the verified behavior",
                    "--resume",
                    "--yes",
                    "--json",
                ]
            )
            == 0
        )
        capsys.readouterr()
    else:
        assert exit_code == 0
    if scenario == "dirty":
        (repo / "README.md").write_text("operator edits\n")
    assert main(["-C", str(repo), "apply", run_id, "--yes", "--json"]) == 0
    assert (repo / "feature.txt").read_text() == "verified\n"
    if scenario == "dirty":
        assert (repo / "README.md").read_text() == "operator edits\n"
    if scenario == "retry":
        assert (repo / "draft.txt").read_text() == "preserved"
    if scenario in ("retry", "input"):
        assert store.read_run().nodes["n1"].attempts == 2
