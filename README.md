# Agent Relay

A router that takes programming tasks, chooses which agent CLI (Claude Code, Codex, Gemini CLI,
Cursor) and model should do each one, runs them as concurrent sub-agents, and passes context and
results between them only as versioned spec files. Two front-ends share one Python core: a
standalone `relay` CLI and an OpenCode plugin (phase 3, not built yet).

## Status

Phase 0 (foundations) is nearly complete; phase 1 (the MVP foreground `relay run`) has not started.
Phase 0 tasks 0.1 and 0.3-0.11 are done. Task 0.2 (the CI workflow) is in progress and has not
been run, so no claim is made that CI passes, and task 0.12 is still todo. The authoritative task
status is in [`docs/development-plan.md`](docs/development-plan.md).

## What works today

- Spec models (pydantic) for task, result, graph, input answers and route entries, with JSON
  schemas generated into `schemas/`.
- `agents.yaml` registry loading with three-level merge, keyed rule merge and predicate rules.
- Run store with atomic spec writes and `run.json`.
- Append-only event log with replay.
- Adapter layer (base protocol) with fake adapters, a fake CLI and scripted scenarios.
- Cross-platform file locks.
- Repository-local git exclude handling.
- `relay doctor`, the only CLI command implemented so far.

Routing, scheduling, worktrees, verification, `relay run`, `relay apply` and the real CLI adapters
are not implemented yet.

## Layout

| Path | Contents |
|---|---|
| `src/relay/` | Python package: specs, registry, store, events, adapters, CLI |
| `tests/` | Test suite |
| `schemas/` | Generated JSON schemas |
| `docs/design.md` | The specification |
| `docs/development-plan.md` | Tasks, milestones and task status |
| `docs/revision-changes.md` | Change history for the documents |
| `plugin/` | Placeholder for the phase 3 OpenCode plugin |

## Development

Python 3.11 or newer. From the repository root:

```sh
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
```

`uv.lock` is not committed; `uv sync` generates it locally.

## Documentation

[`docs/design.md`](docs/design.md) is the specification: how the system works and must behave.
[`docs/development-plan.md`](docs/development-plan.md) holds tasks, milestones and current
status. [`docs/revision-changes.md`](docs/revision-changes.md) records changes to both. This
README stays a summary and does not restate their rules, formats, flags or roadmap.
