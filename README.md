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
sections 7, 9 and 10. `doctor` checks the local environment and configuration only; it never runs
an agent CLI.

Completed task definitions and validation evidence are in the [completed-tasks archive](docs/completed-tasks.md); current status and remaining release work are in the
[development plan task status](docs/development-plan.md#task-status). For sequencing, use the plan's
[remaining-work order](docs/development-plan.md#remaining-work-in-order). Offline tests and recorded outputs
provide different evidence from live runs; see the [fixture guide](tests/fixtures/recorded/README.md).

## Run a task and apply it

Start from this checkout after `uv sync`. Install and authenticate the Claude CLI separately;
the run below invokes that CLI and can incur model usage. Relay does not check the CLI up front:
it runs the configured command when work starts and reports a failure with the command, its exit
code and a redacted stderr tail. Confirm the configured agent with:

```sh
uv run relay agents
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

## Coordinate Claude and Codex in one run

Use a foreground graph for multiple workers. Both CLIs must already be installed and
authenticated. In the target project's `.agents/agents.yaml`, use each CLI's configured model:

```yaml
agents:
  claude:
    models: {fast: default, balanced: default, deep: default}
  codex:
    models: {fast: default, balanced: default, deep: default}
```

For a Python project, save this as `graph.yaml` in that project and commit the configuration
and graph before running. Replace the goals, paths and test command with those for your project:

```yaml
spec_version: 1
commands:
  test: uv run pytest -q
nodes:
  - id: implementation-a
    category: implement
    goal: Implement feature A and its tests under feature_a.
    routing: {agent: claude}
    owns: ["src/feature_a/**", "tests/feature_a/**"]
  - id: implementation-b
    category: implement
    goal: Implement feature B and its tests under feature_b.
    routing: {agent: codex}
    owns: ["src/feature_b/**", "tests/feature_b/**"]
```

From this Relay checkout:

```sh
uv run relay -C ../your-project graph run graph.yaml --concurrency 2 --json
uv run relay -C ../your-project status RUN --json
uv run relay -C ../your-project result RUN implementation-a --json
uv run relay -C ../your-project apply RUN --yes --json
```

Disjoint writers can run concurrently. Overlapping or unknown scopes serialize; add
`depends_on` when one worker needs another's changes. Relay verifies merged output before
publishing the integration branch. The graph stays attached to its foreground process.
Completed-node reuse is allowed only when execution and authentication fingerprints can be
compared safely. The initial comparable case is Claude API-key authentication with `--bare`
and an explicit model; Codex, account/keychain authentication and CLI-default model contexts
conservatively require re-execution. If the attempt cap blocks that re-execution, raise it
without resetting counts:

```sh
uv run relay -C ../your-project resume RUN --max-attempts 3 --json
```

For questions or interruption, use `reply` and `resume`; cancel a live graph with
`relay cancel RUN --yes`. See [graph intake](docs/design.md#14-relay-cli) for the full contract
and [section 6](docs/design.md#6-context-exchange-and-run-store) for the reuse rule, plus the
[development plan task status](docs/development-plan.md#task-status) for active validation and release gates; completed evidence is in the [archive](docs/completed-tasks.md).

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

## Recover an interrupted or failed run

After an interruption, use the recorded run ID to resume. Increase the total attempt cap
explicitly if the interrupted attempt used the available budget:

```sh
uv run relay -C ../relay-demo resume RUN --max-attempts 3 --non-interactive --json
```

To retry a failed run using its recorded task and configuration:

```sh
uv run relay -C ../relay-demo run --retry RUN --max-attempts 3 --non-interactive --json
```

Recovery and its process-identity safeguards are defined in
[section 6](docs/design.md#6-context-exchange-and-run-store). If the blocked reason is
`worker_unconfirmed`, inspect the recorded worker in `.agents/runs/RUN/run.json` and resolve
its termination before retrying. Command flags and exit codes are in
[section 14](docs/design.md#14-relay-cli).

## Conversational interface

On a terminal, bare `relay` (or `relay -C PATH tui`) opens the Textual interface: a
conversation and composer, a task sidebar and an inspector. `relay tui --session S` or
`relay resume --session S` reopens a saved session; it shows the recovered state and never
dispatches anything until you choose to continue. Without a terminal, `relay tui` exits 2 and
the headless commands above are unchanged.

```sh
uv run relay -C ../relay-demo tui
uv run relay -C ../relay-demo tui --session SESSION
```

Questions with options open an answer list: pick an option with the arrow keys and Enter, or
choose **Other (type your own)** to enter a custom answer. A choice question's answer is not
restricted to its options (the same applies to `relay reply`); free-text questions keep a text field.

A typical round: type a request in the composer and press Ctrl+S; answer the coordinator's
questions; review the proposed plan (Ctrl+R) and approve that revision; watch the tasks and
inspect a task's exact handoff, files, diff, checks and logs (Ctrl+N moves between the task list,
conversation and inspector); answer a blocked task from the composer while the others keep
running; then review and apply the cumulative delivery (Ctrl+P, "Review delivery"), which
prepares, verifies and asks for confirmation like `relay apply`. Sending a new request
while a round runs starts **Checkpoint and revise**: admission pauses first, and you choose to
let active work finish or cancel it; nothing replaces a running worker's prompt.

| Key (default) | Action |
|---|---|
| Ctrl+S | Send the composer (an answer to the open question, or a new request) |
| Ctrl+T | Switch the composer between answering and a new request |
| Ctrl+N | Next pane: tasks, conversation, inspector (one pane at a time at 80×24) |
| Ctrl+R | Plan review and approval |
| Ctrl+G | Stop: shows **Stopping** until workers and the coordinator have drained |
| F1 | Shortcut help |
| Ctrl+P | Command palette: every action, bound or not (delivery, sessions, coordinator, Continue round, Cancel task, ...) |
| Ctrl+Q | Quit; while work runs it asks first, then drains before exiting (there is no detach) |

Delivery review, sessions (Ctrl+D deletes, with confirmation), coordinator selection and direct
pane keys have no default key; they are in the palette and the contextual action buttons. Remap
any of them, or unbind one with an empty string, under `tui.keys` in `agents.yaml`:

```yaml
tui:
  keys:
    stop: ctrl+g              # actions: quit send toggle_mode help next_view plan deliver
    deliver: ctrl+l           #   sessions stop coordinator view_conversation view_tasks view_inspector
    view_tasks: f3            # one key, or "ctrl+o,f3" for alternatives; "" unbinds
```

Keys are lowercase names such as `ctrl+r` or `f3`; Enter, Tab, Escape, Ctrl+C and Ctrl+P are
reserved, and two actions cannot share a key. The composer is a text editor, so keys it uses for
editing (Ctrl+A, E, K, U, W, D, X, V, Z, Y) are best left alone.

Ctrl+C does not quit or stop anything (in a Windows console it reaches the interface as a key;
in the composer it copies the selection). Interrupts while the interface drains on exit do not cut the drain
short. A stopped round stays interrupted until you continue it explicitly.

Planning uses an enabled agent that offers a coordinator: one you request, otherwise `claude`,
otherwise the first agent in sorted order, with no qualification record. Without one, saved
sessions stay inspectable and the headless commands above work as before. The interface is
accepted on Windows; Linux and macOS acceptance is still pending. The interaction rules are in
[section 14.1](docs/design.md#141-conversational-terminal-interface-phase-2c).

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
| Agent command unavailable | Check the agent's `cmd` in `agents.yaml` (and `relay agents --json`); the failure names the command, exit code and a redacted stderr tail. |
| No eligible agent (exit 3) | Check `relay agents --json`, the requested category and the permission profiles in section 9. |
| Missing executable verification (exit 2) | Supply your authorized test command with `--check`; see section 10. |
| Verification failed (exit 5) | Inspect the run and result, fix the failing check or implementation, and start a new run. |
| Blocked for input (exit 6) | Follow the reply/resume walkthrough above and inspect the blocked reason. |
| Approval required (exit 7) | Review the prepared operation and use the confirmation command printed by Relay. |
| Dirty starting tree or overlapping edits during apply | Follow the snapshot and delivery rules in section 8.3; preserve your edits before retrying. |
| `apply` says the run is still owned by a running relay process (exit 1) | Wait for that `relay run` to finish (`relay status RUN`), then apply again; nothing was changed. |
| `clean --purge-runs` reports a blocked `relay/<run>/...` ref and keeps the run | A worktree, possibly your main checkout, has that branch checked out. Switch it to another branch, then repeat the purge. |
| Interrupted or failed run | Use the recovery walkthrough above; the recorded attempt count is retained. |

## Local completion gateway

Install the optional server dependencies and start the foreground service:

```sh
uv sync --extra serve
uv run --extra serve relay serve
```

The default listener is `http://127.0.0.1:8765/v1`; `--host ::1` selects IPv6 loopback.
Only loopback listeners are supported. `serve` needs no Git repository and loads built-in
and user configuration once, without the current project's `.agents/agents.yaml`.
`--concurrency`, `--timeout`, `--max-request-bytes` and `--max-output-bytes` control admission
and request resource limits. Stop the foreground process with Ctrl+C.

`GET /v1/models` lists named backend-specific model aliases as `<agent>/<alias>` (for example
`claude/opus`) for each enabled agent with a completion adapter (`claude`, `agy`, `codex`, and
`cursor`). Bare-agent defaults and `relay/auto` remain available for internal routing, but are
not published in the model list. Cursor remains disabled by default. To enable it and list an
account-allowed model, add this to
`~/.config/relay/agents.yaml` in your Relay user configuration. Replace the example model with
one allowed by your Cursor account:

```yaml
agents:
  cursor:
    enabled: true
    models:
      preferred: gemini-3.8-flash-low
```

Gateway configuration is loaded from built-in and user configuration, not the project's
`.agents/agents.yaml`. Claude's named aliases include `sonnet`, `opus` and `fable`; its `default`
alias remains available for internal routing but is not published in the model list.
Codex aliases come from visible slugs in `codex debug models` JSON, and Cursor aliases come from
account-listed IDs in `agent models`. These bounded, read-only inventory queries are cached per
process and make no model calls. A failed query warns and falls back to configured model IDs, then
`default`; it does not disable the backend. An explicit `--model` selection bypasses automatic
inventory. Cursor's `auto` probe may be denied by
your organization's model policy; configure an allowed model and select its `cursor/<model-id>`
entry. Discovery errors leave the alias listed without limits. Repeat `--model ALIAS` to choose
the aliases for the enabled backends. `agy` aliases are enumerated at runtime from `agy models` (TSV
slug and display name), falling back to the `agents.agy.models` values and then to `default`; a
non-`default` alias is passed as `--model <slug>`, and `default` passes no `--model`.
`GET /v1/models` lists named aliases
with `null` context and output limits until they are observed. On the first `/v1/models` request
(never at startup) each uncached alias is probed once with a minimal prompt, and the model the CLI
reports running, with its context window and output limit, is cached in
`~/.config/relay/gateway-models.json`. **Each probe is a short billed model call.** Probe failures
are non-fatal: they are reported on stderr and leave that alias listed without limits.

Pass `--key KEY` or set `RELAY_GATEWAY_TOKEN` before startup to choose the bearer token
(`--key` overrides the environment variable). Otherwise the server generates a token and
prints it once on stderr. Supply that same token to client processes
through their environment; the gateway never writes it into project files. For example,
in a second PowerShell terminal:

```powershell
$env:RELAY_GATEWAY_TOKEN = '<token from the server terminal>'
uv run relay opencode-config --base-url http://127.0.0.1:8765/v1 --refresh
```

On POSIX shells, use `export RELAY_GATEWAY_TOKEN='<token>'` before starting OpenCode.
`opencode-config` builds the same catalog as `serve` locally (it does not contact the server
or need the token) and prints a `provider.relay` JSON snippet with an environment reference for
the token. Without `--refresh` it reads only cached discoveries and makes no model call; every
configured model is still listed, any not yet discovered without limits and named on stderr with
a hint to rerun with `--refresh`, which probes every alias (billed) and updates the shared cache.
Pass the same `--model` options given to `serve`; `--timeout` bounds each probe (default 120 s).
Review and merge the snippet into your OpenCode configuration yourself. Limits appear only where
the CLI reported them, and `tool_call` follows the adapter's completion support.
Generated models offer only reasoning variants present in model inventory. Cursor groups
inventory-listed suffix variants when at least two distinct effort values share a base model ID;
the public catalog shows the base ID once, while original suffixed IDs remain privately resolvable.
Relay accepts `minimal` only for models whose inventory advertises it; unsupported or unavailable
effort values return HTTP 400. The same supported effort can be selected directly by an
OpenAI-compatible client without changing the model ID; for example:

```json
{"model":"cursor/gemini-3.8-flash","reasoning_effort":"low","messages":[{"role":"user","content":"Review this code"}]}
```

OpenCode exposes the inventory-derived variants through its model variant selector. Cursor maps a
base ID and selected effort to the matching original inventory ID; for example, `medium` on
`cursor/gemini-3.7-flash` selects Cursor's `gemini-3.7-flash-medium` model. With no effort selected,
Cursor passes the bare base ID and keeps its native default. A legacy full suffix ID passes through
unchanged unless the request explicitly selects another available effort. Agy aliases remain the
CLI-reported slugs; grouping them remains deferred pending task 5.6.

The configuration fields follow the [OpenCode provider documentation](https://opencode.ai/docs/providers/)
and its [environment substitution syntax](https://opencode.ai/docs/config/#env-vars).

An agent is served by the gateway when it is `enabled` and has a gateway adapter (`claude`,
`agy`, `codex`, `cursor`). Cursor remains opt-in. Codex and Cursor use fixed CLI-mediated
read-only/tool-denial controls, private scratch and denial hooks; these controls are not OS
filesystem isolation. A denied or reported CLI tool action fails the completion; Relay returns
validated client tool proposals but does not execute them. Reasoning effort, output-token limits
and reported metadata are backend-specific: unsupported effort returns an error, token limits
are not claimed unless natively enforced, and unknown model limits stay unknown. There is no
reviewed qualification profile or CLI fingerprint, and Relay does not check
the CLI version, platform or authentication mode up front. The CLI's own failure is reported in
detail when a request runs: the error names the command, the exit code and a redacted stderr
tail, both on stderr and in the OpenAI-style HTTP error body. Client tool calls are validated and
returned to the client; the CLIs do not execute those calls. Output-token enforcement and
reasoning-effort support vary by backend, and unavailable model limits remain unknown. See the
[gateway guide](docs/gateway-qualification.md) for the current behavior and the historical
qualification note. Pinned OpenCode acceptance remains a release gate; installing a CLI alone
does not establish compatibility.

## Layout

| Path | Contents |
|---|---|
| `src/relay/` | Python package: specs, registry, routing, store, adapters, git pipeline and CLI |
| `tests/` | Unit tests, integration tests, platform checks and optional live canaries |
| `schemas/` | Generated JSON schemas |
| `docs/design.md` | Current specification |
| `docs/development-plan.md` | Tasks, milestones and task status |
| `docs/completed-tasks.md` | Archived task definitions and completion evidence |
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
