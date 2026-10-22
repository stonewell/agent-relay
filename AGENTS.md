# AGENTS.md

Entry point for coding agents working in this repository.

## What this repository is

A router that sends programming tasks to different agent CLIs and passes context between them as specs. What it does and how is defined in `docs/design.md`; nothing is described here so that it cannot drift.

## Current state

Phase 0 is being implemented: the Python package is in `src/relay/` and the tests in `tests/`. Which tasks are done is recorded only in the task status section of `docs/development-plan.md`.

## Where things are

| Path | Contents |
|---|---|
| `docs/design.md` | The specification (current design only) |
| `docs/development-plan.md` | Tasks, priorities, milestones and task status |
| `docs/revision-changes.md` | History of changes to the documents above |
| `src/relay/`, `tests/` | Code and tests; layout defined in design section 15 |

## Working here

Tooling (from the repository root):

- Install dependencies: `uv sync` (`uv.lock` is not committed; it is generated locally)
- Build: `uv build`
- Test: `uv run pytest -q`
- Lint: `uv run ruff check .`
- Check formatting: `uv run ruff format --check .`
- Type check: `uv run pyright`

- **Editing any file in `docs/`:** follow the documentation rules below.
- **Implementing a task:** take the task id from `docs/development-plan.md`, read the design sections it names, and record the task's status in the plan. If the design has to change, change `docs/design.md` first, then log the revision.
- **Changing code:** its layout and tooling are defined in the design (section 15); run the test, lint, format and type checks above before finishing.
- Put scratch files and backups outside the repository tree.
- File names are lowercase with hyphens, except the `AGENTS.md` convention.

## Documentation rules

These apply to every file in `docs/`. They exist so that each fact has exactly one home.

### One home per kind of change

| If the change is about... | It goes only in | Never in |
|---|---|---|
| How the system works or must behave: rules, formats, flags, phase scope, tests, risks | `design.md` | the other two files |
| What changed in the documents and why: version summaries, review findings and resolutions, restructures, decisions to drop or move something | `revision-changes.md` | `design.md`, `development-plan.md` |
| Work to be done: tasks, priorities, sizes, dependencies, milestones, exit criteria, task status, cut order | `development-plan.md` | `design.md`, `revision-changes.md` |

A change that touches several kinds is split, not copied:

1. Write the rule once in `design.md`.
2. Add one entry to `revision-changes.md` that says what changed and where, without restating the rule text.
3. Update the affected tasks in `development-plan.md`, referring to design sections instead of repeating them.

### No duplication

- State a rule once, in the section that owns it (see the document map at the top of `design.md`). Everywhere else, refer to it by section number.
- Do not copy tables, lists or sentences between files. If two places need the same information, keep it in one and link or cite it from the other.
- `design.md` contains no version numbers, dates, change history, review findings or task lists. The only exception is its header links to the other files.
- `development-plan.md` contains no design rules; it names the section that defines the behaviour of each task.
- `revision-changes.md` contains no current rules; it describes changes. Section numbers in old entries refer to the design as it was then.
- Before adding text, search the three files for the fact (a unique word or flag name). If it exists, edit it in place.

### `design.md`

- Section numbers are stable references. Do not renumber; add subsections instead. When moving a rule, update every reference and the document map.
- Features that arrive after the first release are tagged in place with their phase and are rejected as unsupported until then.
- Mark anything not confirmed against a real tool as **[verify]**, and remove the mark when a canary or the docs confirm it.
- Every YAML and JSON code block must parse. Check this after editing (a short script that extracts fenced `yaml` and `json` blocks and loads them is enough).
- Keep tables narrow; put long explanations in prose beneath them.

### `revision-changes.md`

- Append new entries at the end, one heading per revision, with a table of changes. Update the "Current versions" table at the top when a document's version changes.
- Bump the design version for any change to `design.md` that alters a rule; wording-only fixes need no bump.
- Record findings with an id, severity and resolution. Reference design sections as of that revision.

### `development-plan.md`

- Task ids are permanent. Never renumber; add new tasks with the next free number in the phase, and mark dropped tasks as dropped in the status section rather than deleting them.
- Every task has a priority (P0, P1, P2), a size, dependencies, the design sections that define it, and a "done when" check.
- Record task status only in the "Task status" section at the end: list tasks that are not `todo`, with status (`in progress`, `done`, `dropped`), date and a one-line note.
- When a task changes scope, edit the task in place and, if the cause was a design change, make the design change first.

## No sensitive information

Nothing in the repository may identify a person, an account or a machine, or grant access to anything. This covers every tracked file (code, tests, fixtures, schemas, docs), commit messages and branch names.

Never commit:

- **Credentials:** passwords, API keys, tokens, cookies, private keys, `.env` contents, auth headers, or connection strings with secrets in them.
- **Personal and account data:** real names, user names, email addresses, account, organization or workspace ids, and session, thread, request or conversation ids from real accounts.
- **Machine data:** absolute local paths (`C:\...`, `/home/...`, `/Users/...`, the scratch and temp directories), OS user names, host names, IP and MAC addresses, drive layouts, installed tool locations, and hardware or serial identifiers.
- **Internal links:** company-internal URLs, host names, ticket ids or chat links.

Use instead:

- **Paths:** paths relative to the repository root, or placeholders such as `<repo>`, `<home>` and `<tmp>`.
- **People and hosts:** `user@example.invalid` and `example.invalid` names.
- **Ids:** obviously fake ones such as `00000000-0000-0000-0000-000000000001`.
- **Credentials:** the name of the environment variable that holds the value, never the value.

What may stay:

- **Generic facts:** OS names and tool versions needed to state compatibility (for example "Windows 11, Codex CLI 0.163.0").
- **Public links:** links to public documentation.
- **Commit identity:** git's own author and committer fields.

**Recorded outputs.** Recorded tool or agent output (such as `tests/fixtures/recorded/`) must be scrubbed of all of the above before it is committed, keeping its structure so the parsers are still tested on the real shape.

**Before committing,** search the staged diff for local paths, user and host names, email addresses, real ids and key-like strings. Fix any hit or replace it with a placeholder. If something sensitive was already committed, tell the user: removing it from history is their decision.
