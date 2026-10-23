# Agent Relay

Agent Relay routes programming tasks to agent CLIs and exchanges context through validated spec
files. A shared Python core supports the standalone `relay` CLI and the planned OpenCode plugin.
The [design](docs/design.md) defines execution, isolation and delivery; its
[phase scope](docs/design.md#16-roadmap) distinguishes foreground single-node execution from
later graph and background execution.

## Getting started

Use Python 3.11 or newer, Git and `uv`. From the repository root:

```sh
uv sync
uv run relay --version
uv run relay --help
uv run relay doctor --json
```

The command reference and request-intake limits are in
[design section 14](docs/design.md#14-relay-cli). Read those limits before attempting a write
run. Agent authentication, permission support and verification policy are defined in design
sections 7, 9 and 10. `doctor` runs model-calling checks only when explicitly selected as described
in the command reference.

Completed tasks, validation evidence and remaining release work are recorded only in the
[task status](docs/development-plan.md#task-status) section. Offline tests and recorded outputs
provide different evidence from live runs; see the [fixture guide](tests/fixtures/recorded/README.md).

## Run a task and apply it

Start from this checkout after `uv sync`. Install and authenticate the Claude CLI separately;
the run below invokes that CLI and can incur model usage. Check that Relay can find it:

```sh
uv run relay doctor --check adapter.claude.version --json
```

Create a sibling demo repository so the walkthrough does not change Relay's source files:

```sh
mkdir ../relay-demo
git -C ../relay-demo init -b main
mkdir ../relay-demo/.agents
python -c "from pathlib import Path; Path('../relay-demo/README.md').write_text('# Greeting demo\n', encoding='utf-8')"
python -c "from pathlib import Path; Path('../relay-demo/.gitignore').write_text('__pycache__/\n', encoding='utf-8')"
```

Save the following as `../relay-demo/.agents/agents.yaml`. The `default` values let the installed
CLI choose its configured model. Configuration merging and the full field reference are in
[design section 3](docs/design.md#3-agent-registry-agentsyaml).

```yaml
agents:
  claude:
    models: {fast: default, balanced: default, deep: default}
```

Commit the starting files, then request a small implementation with an explicit test command:

```sh
git -C ../relay-demo add README.md .gitignore .agents/agents.yaml
git -C ../relay-demo commit -m "Initialize greeting demo"
uv run relay -C ../relay-demo run "Create hello.py with hello(name) returning a greeting, and test_hello.py with unittest cases for normal and empty names." --agent claude --check "python -B -m unittest test_hello -v" --max-attempts 2 --non-interactive --json
```

If Git requests an identity, configure it using your normal Git setup and repeat the commit.
The run prints a permissions banner and a JSON result. Replace `RUN` in the commands below
with its `run_id`. Inspect the result before delivering it:

```sh
uv run relay -C ../relay-demo status RUN --json
uv run relay -C ../relay-demo result RUN --json
uv run relay -C ../relay-demo routes RUN --json
uv run relay -C ../relay-demo apply RUN --yes --json
```

After a successful apply, check the delivered files from the demo repository:

```sh
git -C ../relay-demo status --short
python -B -m unittest discover -s ../relay-demo -p test_hello.py -v
```

The verification and delivery contracts are defined in
[section 10](docs/design.md#10-verification) and
[section 8.3](docs/design.md#83-isolation-and-merging-for-parallel-writers).
For your own project, select it with `-C`, describe your task and supply its test command.
Task-file intake and command overrides are described in
[section 14](docs/design.md#14-relay-cli).

## Answer a blocked task

If the walkthrough exits with code 6, inspect `relay result` for the pending questions. For one
question, replace the answer below with your decision:

```sh
uv run relay -C ../relay-demo reply RUN n1 "Return Hello, followed by the supplied name; reject an empty name." --yes --resume --json
```

For several questions, create `answers.json` in the demo repository using the question IDs
from the result. For example, a result containing `q1` and `q2` could be answered with:

```json
{"q1": "Use a friendly greeting", "q2": "Reject an empty name"}
```

Then submit the file; its path is relative to the repository selected by `-C`:

```sh
uv run relay -C ../relay-demo reply RUN n1 --answers-file answers.json --yes --resume --json
```

If the recorded answers exhaust the attempt limit, explicitly raise the total before continuing:

```sh
uv run relay -C ../relay-demo resume RUN --max-attempts 3 --non-interactive --json
```

Inspect and apply the successful result using the commands above. Revisions, submission IDs,
provenance and limits are defined in
[section 8.2](docs/design.md#82-scheduling); reply flags are in
[section 14](docs/design.md#14-relay-cli).

## Configuration and troubleshooting

| Reference | Where to look |
|---|---|
| `agents.yaml`, merge order and routing/model settings | [Section 3](docs/design.md#3-agent-registry-agentsyaml) |
| Task and result file formats | [Section 5](docs/design.md#5-spec-contract) |
| Permissions and supported enforcement profiles | [Section 9](docs/design.md#9-permissions) |
| Commands, flags and exit codes | [Section 14](docs/design.md#14-relay-cli) |
| Phase scope and current command boundaries | [Sections 16](docs/design.md#16-roadmap) and [14](docs/design.md#14-relay-cli) |
| Outstanding validation and release work | [Task status](docs/development-plan.md#task-status) |

| Symptom | Next step |
|---|---|
| Agent command unavailable | Run `relay doctor --check adapter.claude.version --json` and check the agent's `cmd` setting. |
| No eligible agent (exit 3) | Check `relay agents --json`, the requested category and the permission profiles in section 9. |
| Missing executable verification (exit 2) | Supply your authorized test command with `--check`; see section 10. |
| Verification failed (exit 5) | Inspect the run and result, fix the failing check or implementation, and start a new run. |
| Blocked for input (exit 6) | Follow the reply/resume walkthrough above and inspect the blocked reason. |
| Approval required (exit 7) | Review the prepared operation and use the confirmation command printed by Relay. |
| Dirty starting tree or overlapping edits during apply | Follow the snapshot and delivery rules in section 8.3; preserve your edits before retrying. |
| Interrupted run | Consult section 14's current resume boundary; input resume and interruption recovery have different scopes. |

## Layout

| Path | Contents |
|---|---|
| `src/relay/` | Python package: specs, registry, routing, store, adapters, git pipeline and CLI |
| `tests/` | Unit tests, integration tests, platform checks and optional live canaries |
| `schemas/` | Generated JSON schemas |
| `docs/design.md` | Current specification |
| `docs/development-plan.md` | Tasks, milestones and task status |
| `docs/revision-changes.md` | Document revisions and review resolutions |
| `plugin/` | OpenCode integration directory; see its README |

## Development

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
```

`uv.lock` is generated locally and is not committed. Refresh schemas after changing their models:

```sh
uv run python -m relay.core.specs schemas
```

Repository contribution and documentation rules are in [AGENTS.md](AGENTS.md). Canonical rules,
formats and command contracts stay in the design; implementation progress stays in the plan;
review history stays in the revision log.
