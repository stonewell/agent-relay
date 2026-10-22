# AGENTS.md

Entry point for coding agents working in this repository.

## What this repository is

A router that sends programming tasks to different agent CLIs and passes context between them as specs. What it does and how is defined in `docs/design.md`; nothing is described here so that it cannot drift.

## Current state

Documentation only. There is no source code yet. The first code is phase 0 in `docs/development-plan.md`.

## Where things are

| Path | Contents |
|---|---|
| `docs/design.md` | The specification (current design only) |
| `docs/development-plan.md` | Tasks, priorities, milestones and task status |
| `docs/revision-changes.md` | History of changes to the documents above |
| `docs/AGENTS.md` | Rules for editing the documents (which change goes in which file, no duplication) |

## Working here

- **Editing any file in `docs/`:** read `docs/AGENTS.md` first and follow it.
- **Implementing a task:** take the task id from `docs/development-plan.md`, read the design sections it names, and record the task's status in the plan. If the design has to change, change `docs/design.md` first, then log the revision.
- **When code exists:** its layout and tooling are defined in the design (section 15); add build, test and lint commands to this file when they exist.
- Put scratch files and backups outside the repository tree.
