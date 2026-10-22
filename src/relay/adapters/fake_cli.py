"""Stub agent process for tests: `python -m relay.adapters.fake_cli <scenario-file>`.

It plays the part of an agent CLI. Behaviour comes from the scenario file written by the
`fake` adapter (design section 7.1); it never talks to a model or the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .fake_scenario import Scenario


def _git_commit(cwd: Path, message: str) -> None:
    base = ["git", "-c", "user.name=fake-agent", "-c", "user.email=fake@example.invalid"]
    subprocess.run([*base, "add", "-A"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(
        [*base, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", message],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _result(scenario: Scenario, runtime: dict[str, Any], session_id: str) -> dict[str, Any]:
    if scenario.malformed_result:
        return {"spec_version": 1, "id": runtime["node_id"], "status": "not-a-status"}
    result: dict[str, Any] = {
        "spec_version": 1,
        "id": runtime["node_id"],
        "status": scenario.status,
        "agent": "fake",
        "model": "fake-model",
        "summary": scenario.summary,
        "decisions": scenario.decisions,
        "open_questions": scenario.questions,
        "session": {"cli_session_id": session_id},
    }
    if scenario.status == "needs_input":
        result["question_revision"] = 1
    if scenario.usage:
        result["usage"] = scenario.usage
    return result


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    document = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    scenario = Scenario.model_validate(document["scenario"])
    runtime: dict[str, Any] = document["runtime"]
    node_dir = Path(runtime["node_dir"])
    cwd = Path.cwd()

    prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    (node_dir / "fake-stdin.txt").write_text(prompt, encoding="utf-8")
    observed = {
        "cwd": str(cwd),
        "attempt": runtime["attempt"],
        "resume_session": runtime.get("resume_session"),
        "env": {name: os.environ.get(name) for name in scenario.echo_env},
    }
    (node_dir / "fake-observed.json").write_text(json.dumps(observed), encoding="utf-8")

    for relative, content in scenario.write.items():
        target = cwd / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    for relative in scenario.delete:
        (cwd / relative).unlink(missing_ok=True)
    if scenario.commit is not None:
        _git_commit(cwd, scenario.commit)

    if scenario.sleep_s:
        time.sleep(scenario.sleep_s)
    while scenario.hang:
        time.sleep(0.2)

    session_id = scenario.session_id or f"fake-{runtime['node_id']}-{runtime['attempt']}"
    result = _result(scenario, runtime, session_id)
    if scenario.stdout:
        print(scenario.stdout)
    if scenario.stderr:
        print(scenario.stderr, file=sys.stderr)
    if scenario.output == "json":
        print(json.dumps({"result": result, "session_id": session_id, "usage": scenario.usage}))
    elif scenario.output == "file":
        (node_dir / "result.spec.yaml").write_text(
            yaml.safe_dump(result, sort_keys=False), encoding="utf-8"
        )
    elif scenario.output == "block":
        print("```json")
        print(json.dumps(result, indent=2))
        print("```")
    sys.stdout.flush()
    return scenario.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
