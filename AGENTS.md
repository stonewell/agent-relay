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
