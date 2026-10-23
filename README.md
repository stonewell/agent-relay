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
