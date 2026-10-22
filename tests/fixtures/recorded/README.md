# Recorded agent outputs

Real outputs captured on 2026-10-22 for spike 0.11 (Claude Code 2.1.308, Codex CLI 0.163.0, Cursor agent 2026.10.19,
Windows 11), from one-line tasks in a scratch repository. They are the golden inputs for the
adapter parsers (tasks 1.9 and 1.10). Costs, token counts and the output shape are real. Every
session, thread, request and message id is a fake `00000000-0000-0000-0000-NNNNNNNNNNNN` value (a
resumed run keeps the id of the run it resumes), and local paths are reduced to placeholders or
bare file names, following the sensitive-information rule in `AGENTS.md`.

| File | Command shape |
|---|---|
| `claude_plain.json` | `claude -p --output-format json` |
| `claude_schema_done.json` | `... --json-schema <inline schema>` with a clear task |
| `claude_schema_needs_input.json` | the same with an ambiguous task |
| `claude_result_spec_schema.json` | `--json-schema` with the generated `schemas/result-spec.json` minus `$schema` |
| `claude_resume.json` | `--resume <session_id>` |
| `codex_plain.jsonl` | `codex exec --json -s read-only -` |
| `codex_schema_rejected.jsonl` | `--output-schema` with a non-strict schema (HTTP 400) |
| `codex_schema_needs_input.jsonl`, `..._last_message.txt` | strict schema, ambiguous task, `-o` file |
| `codex_resume.jsonl` | `codex exec resume <thread_id> --json -` |
| `cursor_plain.json` | `agent -p --output-format json --mode plan --trust` (account login, no API key) |
| `cursor_resume.json` | `--resume <session_id>` |
| `cursor_prompt_needs_input.json` | ambiguous task, JSON shape requested in the prompt (no schema flag exists) |
