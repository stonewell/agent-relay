# Agent Relay

A router that takes programming tasks, chooses which agent CLI (Claude Code, Codex, Gemini CLI,
Cursor) and model should do each one, runs them as concurrent sub-agents, and passes context and
results between them only as versioned spec files. Two front-ends share one Python core: a
standalone `relay` CLI and an OpenCode plugin (phase 3, not built yet).

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
