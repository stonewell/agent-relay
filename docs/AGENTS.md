# AGENTS.md: documentation rules

Applies to every file in `docs/`. These rules exist so that each fact has exactly one home.

## One home per kind of change

| If the change is about... | It goes only in | Never in |
|---|---|---|
| How the system works or must behave: rules, formats, flags, phase scope, tests, risks | `design.md` | the other two files |
| What changed in the documents and why: version summaries, review findings and resolutions, restructures, decisions to drop or move something | `revision-changes.md` | `design.md`, `development-plan.md` |
| Work to be done: tasks, priorities, sizes, dependencies, milestones, exit criteria, task status, cut order | `development-plan.md` | `design.md`, `revision-changes.md` |

A change that touches several kinds is split, not copied:

1. Write the rule once in `design.md`.
2. Add one entry to `revision-changes.md` that says what changed and where, without restating the rule text.
3. Update the affected tasks in `development-plan.md`, referring to design sections instead of repeating them.

## No duplication

- State a rule once, in the section that owns it (see the document map at the top of `design.md`). Everywhere else, refer to it by section number.
- Do not copy tables, lists or sentences between files. If two places need the same information, keep it in one and link or cite it from the other.
- `design.md` contains no version numbers, dates, change history, review findings or task lists. The only exception is its header links to the other files.
- `development-plan.md` contains no design rules; it names the section that defines the behaviour of each task.
- `revision-changes.md` contains no current rules; it describes changes. Section numbers in old entries refer to the design as it was then.
- Before adding text, search the three files for the fact (a unique word or flag name). If it exists, edit it in place.

## `design.md`

- Section numbers are stable references. Do not renumber; add subsections instead. When moving a rule, update every reference and the document map.
- Features that arrive after the first release are tagged in place with their phase and are rejected as unsupported until then.
- Mark anything not confirmed against a real tool as **[verify]**, and remove the mark when a canary or the docs confirm it.
- Every YAML and JSON code block must parse. Check this after editing (a short script that extracts fenced `yaml` and `json` blocks and loads them is enough).
- Keep tables narrow; put long explanations in prose beneath them.

## `revision-changes.md`

- Append new entries at the end, one heading per revision, with a table of changes. Update the "Current versions" table at the top when a document's version changes.
- Bump the design version for any change to `design.md` that alters a rule; wording-only fixes need no bump.
- Record findings with an id, severity and resolution. Reference design sections as of that revision.

## `development-plan.md`

- Task ids are permanent. Never renumber; add new tasks with the next free number in the phase, and mark dropped tasks as dropped in the status section rather than deleting them.
- Every task has a priority (P0, P1, P2), a size, dependencies, the design sections that define it, and a "done when" check.
- Record task status only in the "Task status" section at the end: list tasks that are not `todo`, with status (`in progress`, `done`, `dropped`), date and a one-line note.
- When a task changes scope, edit the task in place and, if the cause was a design change, make the design change first.

## Tooling and hygiene

- Keep backups and scratch files outside the repository tree.
- File names are lowercase with hyphens, except the `AGENTS.md` convention.
