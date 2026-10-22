# AGENTS.md

Entry point for coding agents working in this repository.

## What this repository is

A router that sends programming tasks to different agent CLIs and passes context between them as specs. What it does and how is defined in `docs/design.md`; nothing is described here so that it cannot drift.

## Current state

Phase 0 is being implemented: the Python package is in `src/orch/` and the tests in `tests/`. Which tasks are done is recorded only in the task status section of `docs/development-plan.md`.

## Where things are

| Path | Contents |
|---|---|
| `docs/design.md` | The specification (current design only) |
| `docs/development-plan.md` | Tasks, priorities, milestones and task status |
| `docs/revision-changes.md` | History of changes to the documents above |
| `docs/AGENTS.md` | Rules for editing the documents (which change goes in which file, no duplication) |
| `src/orch/`, `tests/` | Code and tests; layout defined in design section 15 |

## Working here

Tooling (from the repository root):

- Install dependencies: `uv sync --locked`
- Build: `uv build`
- Test: `uv run pytest -q`
- Lint: `uv run ruff check .`
- Check formatting: `uv run ruff format --check .`
- Type check: `uv run pyright`

- **Editing any file in `docs/`:** read `docs/AGENTS.md` first and follow it.
- **Implementing a task:** take the task id from `docs/development-plan.md`, read the design sections it names, and record the task's status in the plan. If the design has to change, change `docs/design.md` first, then log the revision.
- **Changing code:** its layout and tooling are defined in the design (section 15); run the test, lint, format and type checks above before finishing.
- Put scratch files and backups outside the repository tree.
