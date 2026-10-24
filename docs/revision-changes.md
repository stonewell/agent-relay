# Revision changes

The history of changes to the project documents: version summaries, review findings and resolutions, and restructures. It is the only place that records *what changed and why*; the documents themselves state only the current design (`design.md`) and the current tasks (`development-plan.md`). Which file holds what is defined in the documentation rules of the root `AGENTS.md`.

Section references (`section N`) in older entries point into `design.md` as it was at that revision, and may have moved since. The per-version regression scenarios are consolidated into the test plan in design.md section 16; the original per-version wording is kept here for traceability.

## Current versions

| Document | Version |
|---|---|
| `design.md` | v0.28 |
| `development-plan.md` | v0.9 |
| `AGENTS.md` | v4 |

## Version summaries

v0.10 review: git-local exclusion of runtime state, Windows path-length handling, estimated-cost labeling for defaulted pricing rates, unverified session-id fields marked, complete event catalog, default escalation order, approval-required exit code, defined `risk`/`rerouted`/`cost_ceiling` inputs, sensitive-file confirmation for `--allow-dirty`, and a precise reserved-path rule. Findings and resolutions are recorded in section 24.

v0.9 review: specified synthetic base commits and baseline-aware overlap resolution for `--allow-dirty` runs; added strict validation rejecting reserved runtime paths in task specs; defaulted omitted cache pricing rates; addressed long Unix domain socket path limits via temp fallback; expanded toolchain and Windows SSH environment allowlists; formalized rule predicate evaluation and escalation ceiling behavior; documented CLI session ID extraction; and added application events to the canonical event catalog. Findings and resolutions are recorded in section 23.

v0.8 review: replaced endpoint-only ownership with native lifetime locks; corrected the same-user IPC and read-only enforcement claims; specified verification and recovery for application transactions, answer/retry semantics, and runtime-metadata exclusions. Findings and resolutions are recorded in section 22.

v0.7 review: Claude `--bare` auth caveat, supervisor lock mechanism, IPC threat model (peer checks, no worker-reachable privileged commands), honest Windows `workspace` sandbox statement, `orch apply` with dirty trees and shadow-git three-way sync, proxy/CA/config-home environment allowlists, `agent_rule`/`model_rule` in task specs, roadmap placement of supervisor/apply/reply, and assorted consistency fixes. Findings and resolutions are recorded in section 21.

v0.6 review: added verified delivery application (`orch apply` / `task_apply`), interactive unblocking for `needs_input` (`orch reply` / `task_input`), cross-platform environment allowlists, language resolution hierarchy, `git worktree remove` lifecycle with `refs/orch/...` namespaces, split attempt/node timeout budgets, cache write pricing, and local IPC transport specs. Findings and resolutions are recorded in section 20.

v0.5 review: clarified supervisor lifetime and crash recovery, conservative write overlap detection, merge ancestry, graph/result states, durable routing records, rule ordering, and token accounting.

v0.4.1: flag table (section 7.2) re-verified against local `--help` (Claude 2.1.308, Codex 0.162.1, Cursor 2026.10.19) and Gemini docs; corrections listed in section 7.2 and 17.

v0.4 changes: added the route log (section 4.4) and id-keyed agent-selection rules (`routes.rules`, section 3); `run_agent_task` now defaults to `wait: false`; cost fields marked phase 4; pricing marked illustrative; shadow-git baseline clarified.



## Review v0.5

Review scope: internal consistency and execution/recovery contracts in the v0.4.1 draft. The CLI flag inventory was retained; this review does not constitute new live validation of third-party flags or sandbox guarantees. The following findings are addressed in this revision; the corresponding regression scenarios are specified in section 16 and remain implementation work.

| ID / severity | Finding in v0.4.1 | Resolution |
|---|---|---|
| R1 / P1 | PID + heartbeat takeover could allow two supervisors to execute or publish concurrently; crash recovery did not define interrupted publication. | Lifetime OS lock, process-start identity, owner generations, worker reconciliation, and idempotent output publication (section 6). |
| R2 / P1 | `wait: false` returned before completion without defining who kept runs alive; generic tool abort handling could cancel on wait expiry. | Independent per-run supervisor, readiness acknowledgement, authenticated IPC, and distinct observer timeout versus explicit cancellation (sections 8.6 and 13.1). |
| R3 / P1 | Narrow `owns` could permit parallel scheduling while broader `diff_scope` allowed both writers to modify the same secondary files. | Reserve effective allowed write scopes and serialize unknown/overlapping intersections, including unspecified whole-repository scopes (section 8.3). |
| R4 / P1 | Applying the ordinary single-parent commit rule to conflict resolutions could discard dependency ancestry and reintroduce conflicts at later fan-ins. Pruning also lacked immutable ref retention. | Preserve original merge parents and retain per-attempt/merge refs until explicit purge (section 8.3). |
| R5 / P1 | `needs_input`, `continue-all`, cancellation of pending nodes, and retry limits across fallback lacked consistent scheduler outcomes. | Validate graph mutations; map input requests to blocked nodes, reject unsupported failure policy, enforce total attempts, and return nonzero for blocked runs (sections 8.1–8.4 and 14). |
| R6 / P2 | Optional route-log writes could fail while events stored only a pointer, losing the explanation permanently; privacy mode omitted meaningful operational fields and did not cover free-text reasons. | Canonical complete routing events, rebuildable projections, explicit dispatch failure on durable-store errors, and consistent privacy handling (section 4.4). |
| R7 / P2 | Appending project rules after first-match built-ins could make exceptions unreachable; partial predicate merges and anonymous IDs made overrides ambiguous. | Required unique rule IDs, replacement predicate/action blocks, explicit priority, and stable ordering (sections 3–4). |
| R8 / P2 | Exhausted or temporarily busy preferred candidates had no defined route; a busy pinned agent could be treated as unavailable. | Eligibility filtering, capacity queueing, explicit score fallback for exhausted rules, and strict pin behavior (section 4.2). |
| R9 / P2 | Pricing could charge cached input twice when provider input totals already included cached tokens. | Normalize disjoint token buckets, snapshot rate provenance, and label incomplete billing data as estimated/unknown (section 8.5). |


## Review v0.6

Review scope: lifecycle boundaries, interactive human-in-the-loop completion, cross-platform runtime stability, and git worktree administrative hygiene in the v0.5 draft.

| ID / severity | Finding in v0.5 | Resolution |
|---|---|---|
| F1 / P1 | No command existed to apply verified delivery commits to the user's active branch or workspace; shadow-git mode had no mechanism to consume deliverables. | Added `orch apply <run>` and OpenCode tool `task_apply`, safely fast-forwarding or merging `orch/<run>/integration` into the active branch after verifying a clean working copy, or syncing files in shadow-git mode (sections 1, 8.3, 13.1, 14). |
| F2 / P1 | Nodes reporting `needs_input` transitioned to `blocked`, but neither the CLI nor the plugin provided a tool or command to submit operator answers. | Added `orch reply <run> <node> "<answers>"` and OpenCode tool `task_input`; answers are persisted in `input_answers.yaml` in the node directory, injected into the follow-up prompt, and re-enqueue the node as `pending` (sections 5.4, 8.1, 13.1, 14). |
| F3 / P1 | Environment allowlist stripped platform-essential variables (`SystemRoot`, `PATHEXT`, `ComSpec`), causing Windows child processes (runtime sockets, node spawns, `.cmd` shims) to fail silently. | Defined explicit cross-platform base allowlist: Windows (`SystemRoot`, `WinDir`, `ComSpec`, `PATH`, `PATHEXT`, `TEMP`, `TMP`, `LOCALAPPDATA`, `USERPROFILE`) and Unix (`PATH`, `HOME`, `USER`, `SHELL`, `TMPDIR`, `LANG`, `LC_ALL`) (section 9.4). |
| F4 / P2 | `language` was tied strictly to `repo.language`, causing polyglot repositories to misroute tasks written in secondary languages. | Defined hierarchical language resolution: explicit `task.language` -> detected dominant extension in `context.files`/`owns` -> `repo.language` fallback (sections 4.3, 5.2). |
| F5 / P2 | Worktree pruning via directory deletion left dangling administrative metadata in `.git/worktrees/`, risking git command slowdowns and lock contention. | Required `git worktree remove --force` followed by `git worktree prune`, and isolated attempt/merge refs under the `refs/orch/<run>/...` namespace (sections 6, 8.3). |
| F6 / P2 | Cumulative `timeout_s` across attempts meant an attempt timing out at the limit left 0s for retries or escalations. | Disambiguated `budget`: `timeout_s` governs per-attempt execution timeout, while `max_duration_s` enforces cumulative wall-clock node duration (section 5.2). |
| F7 / P2 | Token billing omitted prompt cache creation (write) pricing buckets, systematically underestimating Anthropic Claude costs. | Added `cache_write_per_m` to the pricing schema in `agents.yaml` and normalized the pricing settlement formula (sections 3, 8.5). |
| F8 / P2 | Local supervisor IPC lacked concrete transport definitions and authentication boundaries. | Specified Unix domain sockets (`ipc.sock`, 0600) on Unix/macOS and named pipes (`\\.\pipe\orch-<run-id>`) with user-only ACLs on Windows, authenticated by a high-entropy bearer token (`ipc.token`, 0600) (section 8.6). |


## Review v0.7

Review scope: the v0.6 draft, checked for implementability on the reference (Windows) machine, security of the supervisor/IPC boundary, and internal consistency. Flag checks were not repeated beyond the Claude `--bare` auth caveat taken from the local `claude --help`.

| ID / severity | Finding in v0.6 | Resolution |
|---|---|---|
| G1 / P1 | `--bare` is recommended for reproducible Claude runs, but under `--bare` only `ANTHROPIC_API_KEY`/`apiKeyHelper` authenticate; subscription (OAuth/keychain) logins fail. | `probe()` records the auth mode; `--bare` is used only with API-key auth (section 7.2). |
| G2 / P1 | "Exclusive OS lock" had no mechanism; Node has no file locking and mtime-based lock libraries are the heartbeat takeover the design forbids. | Ownership by exclusive creation of the IPC endpoint (Unix socket bind, Windows first pipe instance), leftover-socket probing, `run.lock` diagnostic only (section 6). |
| G3 / P1 | Workers at `full` can read `ipc.token` and call IPC (cancel, reply, apply); 0600 is meaningless on Windows. | Threat model plus observer/privileged command split, peer-process check against worker trees, user-presence confirmation for privileged commands, no secrets in worker env, residual risk stated (section 8.6). |
| G4 / P1 | Windows Job Objects were listed as a `workspace` sandbox, but they do not restrict file access. | Only filesystem-confining mechanisms qualify; native Windows `workspace` reported unsupported until proven; roadmap expectation set (sections 9.3, 16). |
| G5 / P1 | `orch apply` required a clean tree, which contradicts `--allow-dirty` runs; shadow-git sync ignored deletions, renames and concurrent user edits. | Path-level overlap check, disjoint dirty edits preserved, `--branch`, shadow-git three-way sync with `.orch-incoming` conflicts and idempotent `apply.finished` (section 8.3). |
| G6 / P3 | Header pointed to section 19 for v0.6 findings (they are in section 20). | Corrected. |
| G7 / P2 | Risk row said agents "listen for completion events" and named only `run_task_graph`, contradicting section 13.1. | Aligned: both tools default to `wait: false`; poll `task_status` (section 17). |
| G8 / P2 | Run tree omitted `ipc.*`, `run.lock`, `input_answers.yaml` and the `refs/orch` refs. | Added to section 6. |
| G9 / P2 | Environment allowlist omitted Windows paths (`APPDATA`, `ProgramFiles`, ...), Unix `XDG_*`/`TERM`, proxy and CA variables, and CLI config homes. | Extended base allowlist; proxy URLs never logged; supervisor secrets never forwarded (section 9.4). |
| G10 / P2 | Task spec example used a nonexistent `rule_id: codex-default`; field names differed from the route log. | `routing` now carries `agent_rule` and `model_rule` everywhere (sections 4.3, 5.2). |
| G11 / P2 | Roadmap omitted the supervisor, IPC, `orch apply` and `orch reply`; `budget_exhausted` blocked nodes had no resume path. | Placed in phases 1 and 2; added `orch resume --raise-budget` (sections 14, 16). |
| G12 / P3 | Read-only node list differed between sections 8.1 and 8.3. | Both now say `read` access covers research, review, debug and architecture. |
| G13 / P3 | `defaults.timeout_s` did not say per-attempt vs cumulative. | Commented as per attempt; cumulative limit is `budget.max_duration_s` (section 3). |


## Review v0.8

Review scope: execution ownership, delivery correctness/recovery, permission guarantees, and input lifecycle in v0.7. Earlier review tables are historical; the current normative sections supersede their original resolutions. Native locking and IPC lifecycle references were checked against the sources in section 18. Agent CLI flags and runtime behavior were not revalidated. The resolutions below are design changes, with implementation regression scenarios in section 16.

| ID / severity | Finding in v0.7 | Resolution |
|---|---|---|
| H1 / P1 | Endpoint binding plus a failed connection probe is not a complete lifetime lock: stale-socket unlink/rebind races could admit competing supervisors, and the required Windows pipe behavior lacked a portable runtime implementation. | Require an in-process native lifetime-lock binding before endpoint cleanup or state mutation; retain lock objects outside purgeable runs and capability-test the filesystem/runtime (sections 6, 15). |
| H2 / P1 | The IPC section described confirmation as outside a full worker's reach even though a same-user worker can invoke `orch ... --yes`; peer-tree checks cannot attest human intent. | State the actual same-user trust boundary, bind workflow approval to operation digests, provide a host-approval fallback, and make no isolation claim for prompts/tokens/peer checks at `full` (section 8.6). |
| H3 / P1 | Applying onto a newer target branch could create a textually clean but untested merged tree; an in-place merge could leave the user's checkout conflicted. | Prepare and verify the target-specific tree in a disposable checkout, then recheck target/index/content identities before applying; `--branch` creates a verified branch without switching the user's checkout (section 8.3). |
| H4 / P1 | Shadow-git applied files incrementally without a durable recovery plan; a crash or later conflict could leave partial changes, overwrite `.orch-incoming` files, and break run-level idempotence across different targets. | Preflight all paths, default to no writes on conflicts, retain before-images and per-path progress in a durable operation journal, recover by content identity, and track completion per operation/target (section 8.3). |
| H5 / P1 | The Windows fallback advertised `readonly` based on a CLI mode and post-run immutability check, contradicting the guarantee to prevent mutating commands and never widen permissions. | Require a complete enforcing profile for `readonly` as well as `workspace`; post-run detection alone is insufficient, and some adapters initially expose only `full` (sections 9.3, 16). |
| H6 / P2 | A final-attempt `needs_input` answer was always re-enqueued despite an exhausted total attempt budget; answer revisions, provenance and cache invalidation were undefined. | Preserve revisioned/idempotent answers, include them in the input key, enforce remaining limits before enqueue, and require explicit attempt-limit increases; distinguish operator input from model-submitted content (sections 5.4, 8.2, 13.1, 14). |
| H7 / P1 | Creating runtime state inside the source directory could dirty subsequent runs or recursively include shadow-git objects and worktrees in new baselines/deliverables. | Explicit reserved-runtime filters for snapshot, dirty-check, capture and application paths; isolated shadow staging/object store; retain project configuration as an input (section 6). |


## Review v0.9

Review scope: input snapshot generation under dirty workspaces, delivery collision prevention, reserved path containment, pricing defaulting, platform transport boundaries, environment allowlists, and rule predicate formalization in the v0.8 draft.

| ID / severity | Finding in v0.8 | Resolution |
|---|---|---|
| J1 / P1 | Under `--allow-dirty`, the base commit mechanism was unspecified; pinning `HEAD` would discard uncommitted user changes from root worker checkouts. | Create a synthetic base commit (`refs/orch/<run>/base`) from the captured dirty snapshot tree parented by `HEAD` for root node `input_commit` (sections 8.3, 9.4). |
| J2 / P1 | `orch apply` compared working copy status strictly against `HEAD`, causing `--allow-dirty` deliveries to falsely report user edits as conflicting overlaps. | Recognize working tree files whose content and mode match the captured baseline snapshot as base state rather than conflicting concurrent edits (section 8.3). |
| J3 / P1 | Task specs and dynamic `spawn_requests` lacked explicit constraints preventing `owns` or `diff_scope` from matching internal runtime directories. | Strictly reject task specs whose `owns` or `diff_scope` target or intersect with reserved subtrees (`.agents/**`, `.git/**`) during graph validation (sections 8.1, 8.4). |
| J4 / P2 | Models omitting `cache_read_per_m` or `cache_write_per_m` (e.g. Codex, Gemini) caused token settlement calculation to evaluate `NaN` or unexpected rates. | Default omitted cache pricing rates to `input_per_m` (no discount or surcharge) in the pricing formula and schema (sections 3, 8.5). |
| J5 / P2 | Long repository working paths caused Unix domain socket paths (`.agents/runs/<run-id>/ipc.sock`) to exceed OS `sun_path` limits (104/108 bytes) and fail `bind()`. | Fall back to `$TMPDIR/orch-<repo-hash>-<run-id>.sock` (0600) when the canonical path exceeds 100 bytes, recording the actual path in `ipc.endpoint` (section 8.6). |
| J6 / P2 | Environment allowlist omitted `SSH_AUTH_SOCK` on Windows and common toolchain roots (`JAVA_HOME`, `CARGO_HOME`, etc.), causing cross-platform build/fetch failures. | Added `SSH_AUTH_SOCK` and `GIT_SSH_COMMAND` to Windows allowlist; pass common toolchain roots when present in the parent environment (section 9.4). |
| J7 / P2 | When `escalate_on_failure` reached the top model tier (or for literal model IDs), retry escalation behavior was undefined. | Subsequent retries remain on the current/highest model until `max_attempts` is exhausted or cross-agent fallback triggers (section 4.3). |
| J8 / P2 | Rule predicate (`when`) evaluation lacked formal conjunction, array disjunction, and comparison operator semantics. | Formally defined: conjunctive attributes (AND), disjunctive array values (OR), and comparison operators (`<`, `<=`, `>`, `>=`, `=`) on numbers and ordered complexity (section 3). |
| J9 / P2 | Extraction of `session.cli_session_id` was referenced for resume but omitted from the adapter output parsing specification. | Added explicit session ID extraction from CLI payloads (Claude JSON `session_id`, Codex JSONL `thread_id`/`session_id`, Cursor JSON `chatId`/`session_id`) in section 7.3. |
| J10 / P2 | Event catalog in Section 11 omitted application lifecycle events (`apply.started`, `apply.finished`). | Added `apply.started` and `apply.finished` to the formal event catalog in section 11. |


## Review v0.10

Review scope: the v0.9 draft, for implementability on the Windows reference machine, consistency of cost labeling and event/exit-code catalogs, and undefined rule inputs. Items deferred to a separate decision: the native lock binding runtime matrix (section 15), MVP slicing (section 16), and moving review history out of this document.

| ID / severity | Finding in v0.9 | Resolution |
|---|---|---|
| H1 / P2 | Runtime state under `.agents/` appeared in the user's `git status`, nested worktrees looked like nested repositories, and `git add -A` could stage them; exclusions applied only to orchestrator captures. | Marked block in the repository-local exclude file (`info/exclude`), never `.gitignore`; `doctor` verification; shadow-git unaffected (section 6). |
| H2 / P2 | Nested worktrees plus deep project paths exceed the Windows 260-character limit. | Short out-of-repo Windows worktree root with truncated, hashed node ids, per-worktree `core.longpaths`, `doctor` long-path canary, `paths.worktree_root` setting (sections 3, 8.3). |
| H3 / P2 | Section 3 defaulted omitted cache rates to `input_per_m` while section 8.5 forbids presenting missing rates as exact; defaults can under/over-state cost. | Placeholder arithmetic is allowed but the cost is labeled `estimated`, cannot qualify for hard mode, and `doctor` warns (sections 3, 8.5). |
| H4 / P2 | v0.9 added Codex/Cursor session-id field names without confirmation. | Marked **[verify]**; per-adapter canary records observed names; fresh-session fallback with captured state (section 7.3). |
| H5 / P2 | Event catalog omitted supervisor start/recovery, node state transitions and block reasons, attempt phases, input records and apply progress needed by recovery and audit. | Full catalog by category; answer content kept out of events (section 11). |
| H6 / P3 | `escalate_on_failure` was defined only for Claude. | Default is the agent's `models` order; single-tier agents never escalate (section 4.3). |
| H7 / P3 | No exit code for a prepared-but-unconfirmed privileged operation. | Exit `7` (approval required), no state change, prints the confirming command; `approval_required` status for tools (section 14). |
| H8 / P3 | `risk` and `cost_ceiling` were used as rule inputs without definitions, and `rerouted` was never produced. | `risk` defined from configurable path globs/keywords with a complexity bump; `cost_ceiling` is phase 4 and rejected earlier; `rerouted` defined for resume/config/answer-driven changes (sections 3, 4.1, 4.3, 4.4). |
| H9 / P3 | `--allow-dirty` snapshots could copy untracked secrets into git objects and worktrees without notice. | Sensitive-path scan and confirmation, `--exclude`, refs never pushed and purged with the run (section 8.3). |
| H10 / P3 | Reserved-path rule rejected all of `.agents/**`, including the human-owned `.agents/agents.yaml`. | Reject `.git/**` and the enumerated runtime subtrees only; config edits are allowed and never alter a running run (section 8.1). |

## Restructure v0.11

Not a findings pass; a reorganization requested after the v0.10 review.

| Change | Detail |
|---|---|
| MVP sliced | Phase 1 is a foreground single-node `orch run` with routing, route log, worktree capture, verification and git-mode `orch apply`. Supervisor, IPC, task graphs, `needs_input`, shadow-git, plugin, Gemini/Cursor and cost features are explicitly out of the MVP (design section 16). |
| Native lock moved to phase 2 | Phase 1 uses an interim exclusive-create lock with PID plus process-start identity; the native lifetime lock gates only the detached supervisor, with a phase-0 spike deciding the binding and runtime matrix (design sections 6, 15). |
| Document reorganized | Version summaries and the review tables (sections 19 to 24 of v0.10) moved to this file; a document map names the canonical section for each topic; a phase-availability note and in-place phase tags were added; the version-labelled regression bullets were consolidated into an area-organized test plan with phase tags (design section 16). The per-version wording of those bullets was dropped from this log in v0.13 because the consolidated test plan in `design.md` is the single copy. |
| Shadow-git and journaled sync | Deferred to phase 4; git mode in phases 1 to 3 relies on git's merge plus an expected-ref update. |

## Consolidation v0.11

Duplicate statements of the same rule were reduced to one canonical section, with other sections pointing to it.

| Rule | Canonical section | Duplicates reduced |
|---|---|---|
| `needs_input` protocol (blocking, answers, trust labeling, limits) | 8.2 | 5.4, 8.1 |
| Reserved paths and runtime-state exclusion | 6 | 8.1, 8.4, a repeated shadow-git sentence in 6 |
| Permission enforcement and what qualifies as a profile | 9.3 | 7.2 table cells and note, a Claude-specific sentence in 9.3, 17 |
| Wait and cancel semantics | 8.6 | 13.1 (two bullets merged into one pointer), 17 |
| `orch apply` behaviour and flags | 8.3 | 14 (CLI comment), 13.1 (tool row), 17 |
| `--allow-dirty` snapshot semantics | 8.3 | 9.4 |
| Pricing labeling and hard-mode consequences | 8.5 | 3, 17 |
| Run ownership and locks | 6 | 17 (three rows merged into one) |
| Risk register | 17 | reduced from 38 to 28 rows by merging duplicates: worktree accumulation, restricted profiles, env allowlist, delivery, route log, cost, session ids |

A document map at the top of the design lists the canonical section per topic. Not consolidated on purpose: the tests (organized by area, section 16), the event catalog (section 11), and per-CLI flag details (section 7.2).

## Prioritization v0.12: `needs_input` moved into the MVP

| Change | Detail |
|---|---|
| Roadmap | `needs_input` moved from phase 2b to phase 1 as a foreground feature: typed questions, inline terminal answers, or `orch reply` plus `orch resume` for non-interactive runs. Phase 2b keeps only the background variant (replies through IPC, dependents of a blocked node held). |
| Commands | `reply` and single-node `resume` available in phase 1; `--non-interactive` added to `orch run`. |
| Spec | `open_questions` gets a typed schema with a question revision (section 5.3); phase 1 behaviour and limits are defined in section 8.2. |
| Tests and risks | New foreground `needs_input` test area; new risk row for agents that do not follow the result-spec protocol, with a `doctor` canary. |

## Documentation governance v0.13

| Change | Detail |
|---|---|
| Single home per kind of change | Design rules go only to `design.md`; revision history, findings and restructures only to this file; task breakdown, priorities and task status only to `development-plan.md`. Rules are in `docs/AGENTS.md`. |
| File renamed | `review-log.md` is now `revision-changes.md`. |
| Duplicates removed | Version number and date removed from the `design.md` and `development-plan.md` headers (versions live in the table above); the per-version regression scenarios dropped from this log (the test plan in `design.md` is the single copy); the phase 0 spike list in `design.md` section 16 reduced to a pointer, since the spikes are tasks in `development-plan.md`. |
| New documents | `AGENTS.md` (project entry point for agents) and `docs/AGENTS.md` (documentation rules). `development-plan.md` v0.1 created from design phases 0 to 5. |

## Implementation language v0.14

| Change | Detail |
|---|---|
| Core and CLI in Python | The TypeScript/Node/Bun core is replaced by Python 3.11+ (pydantic, asyncio, `uv`, `pytest`, `ruff`, `pyright`). TypeScript remains only for the OpenCode plugin, which OpenCode requires. Reason: no Node runtime for `orch` users, no native lock binding, and the subprocess-bound workload does not need a compiled language. Rust and Go were evaluated and recorded in section 12 as the revisit path. |
| Sections touched | 1 (assumption), 2 (diagram and dependency rule), 4.4 and 5 (schema source), 6 (lock mechanism), 7.2 (Windows `.cmd` spawning), 12 (alternatives), 13.3 (plugin packaging), 15 (rewritten), 16 and 17 (spike and risk wording), 18 (IPC reference). |
| Plan | `development-plan.md` v0.2: tasks 0.1, 0.2, 0.3, 0.10 (Spike A reduced to M to S and re-scoped), 3.7, release row and one risk row updated. Task ids unchanged. |
| Section 5.5 added | Format of `input_answers.yaml` (submissions, provenance values, answer hash) was referenced by sections 6, 8.2 and 13.1 but never defined; found while implementing task 0.3. Sections 5 intro and the document map unchanged in meaning. |
| Section 3 clarified | While implementing task 0.4: removed the undefined category `triage` from the example rule `claude-cheap`; defined the built-in defaults (no pricing, `default` model tier for Codex and Gemini), strict rejection of unknown keys, the permitted agent keys and the `when` attributes. |
| Sections 6 and 11 clarified | While implementing tasks 0.5 and 0.6: run id format and reuse rule, node directory naming, `run.json` contents and run states; the flat `events.jsonl` record shape, durability on append and torn-tail rules. |
| Section 7.1 | Adapter interface restated as a Python `Protocol` (it was TypeScript pseudocode); the `fake` adapter and its stub agent process described, for task 0.7. |
| Section 14 | `orch doctor` semantics defined for task 0.8: check statuses, stable ids, `paid` checks gated behind `--paid`, exit codes. |
| Section 15 | Spike A result for task 0.10: supported platform matrix, the lock and IPC mechanisms, the exec-only worker rule and the no-unlink rule. Replaces the sentence that promised the spike. |

## Spike B results v0.14

Task 0.11 ran about ten one-line prompts against Claude Code 2.1.308 (four paid runs, roughly $0.60 in total, mostly context overhead) and Codex 0.163.0 in a scratch repository. Recorded outputs are in `tests/fixtures/recorded/`; the findings are executable in `tests/test_recorded_outputs.py`.

| Finding | Resolution |
|---|---|
| Claude rejects a schema carrying `$schema` (`no schema with key or ref ...`) | Adapter strips it; section 5.4 and the flag table updated. |
| Codex `--output-schema` accepts only strict schemas (all properties required, `additionalProperties: false`); a non-strict schema fails with HTTP 400 before generation | Adapter converts the generated schema and drops nulls from the reply; section 5.4. Tasks 1.10 and 1.11 carry it. |
| Codex `input_tokens` includes cached tokens; Claude's buckets are disjoint | Already covered by the disjoint-bucket rule in section 8.5; recorded as tests. |
| Claude ran a different model than the one requested (`--model haiku` ran as `claude-sonnet-5-5` with the local user config) | Adapter records the model from `modelUsage`; no design change. |
| Both CLIs returned typed `needs_input` for an ambiguous task | Go for the `needs_input` approach (risk row in section 17 updated, with the sample-size caveat). |
| Session ids: Claude `session_id`, Codex `thread_id`; resume keeps the id and context | `[verify]` removed for both in section 7.3. Cursor not run (no API key): stays `[verify]`. |
| Not verified: Codex reasoning-effort config key; Cursor and Gemini behaviour | `[verify]` tags remain. |

## Spike B addendum (Cursor) v0.14

Three more runs on Cursor `agent` 2026.10.19, authenticated by account login (no `CURSOR_API_KEY` needed, contrary to the assumption in section 7.2).

| Finding | Resolution |
|---|---|
| Session id is `session_id`; resume keeps id and context | `[verify]` removed in section 7.3. |
| Usage is camelCase tokens with disjoint cache buckets and no cost | Fixture and test; cost comes from pricing. |
| No schema flag: JSON came back as text in `result`, with `options: []` on free questions and `null` defaults | Parser normalisation rule added to section 5.4. |
| The ambiguous-task run took 242 s | Noted for timeouts: Cursor attempts can be slow even without edits. |

## Phase 0 code review v0.15

A review of the uncommitted fixes to tasks 0.3 and 0.7 found that the reserved-path check depended on the host and that the retry fix lived in the fake adapter only.

| Id | Severity | Finding | Resolution |
|---|---|---|---|
| R15-1 | High | A retry deleted the previous `result.spec.yaml`, losing a `needs_input` question set, and only the fake adapter did it | Section 8.2: the orchestrator moves earlier results to `attempts/NN/` for every adapter; section 6 layout lists the directory. |
| R15-2 | High | Case folding used the host's rules, so `.GIT/**` passed on macOS and one spec validated differently on Windows and Linux | Section 8.1: the check folds ASCII case on every host. |
| R15-3 | Medium | Windows-equivalent spellings (trailing dots or spaces, stream suffixes, 8.3 short names) bypassed the check | Section 8.1: those spellings are compared as the name they resolve to. |
| R15-4 | Low | Path canonicalisation dropped a trailing slash; malformed node-dir or stdout results raised raw errors | Code only: trailing slash kept; parse errors raise `ResultInvalid` (section 7.3 unchanged). |

A second review of the same changes found that the R15-2 and R15-3 resolutions were still incomplete.

| Id | Severity | Finding | Resolution |
|---|---|---|---|
| R15-5 | High | A `*` segment after a literal one stopped the check, so `.agents/*/**` and `*/runs/**` were accepted | Section 8.1: a scope is rejected when it can reach a reserved path through a non-wildcard segment; the exemption is limited to scopes made only of `*` and `**`. |
| R15-6 | High | Short names were caught only in their literal form (`GIT~?`, `*~1` and hashed names passed), and folding case or cutting at `:` broke glob classes | Section 8.1 replaces the R15-3 rule: any segment with `~` before a digit or glob character, or with `:`, is treated as possibly reserved; case is ignored in the match instead of folding the glob. |
| R15-7 | Medium | Archiving could overwrite an earlier archived result | Section 8.2: archived files are never overwritten. |
| R15-8 | Low | `.` and `./` behaved differently; unreadable result files raised raw errors; results were validated twice; Windows sharing violations were not retried during archiving | Section 8.1: the repository root itself is rejected. The rest is code only. |

## Sensitive-information rule (AGENTS v2)

| Change | Detail |
|---|---|
| Root `AGENTS.md` | New section "No sensitive information": what must never be committed (credentials, personal and account data, machine data, internal links), the placeholders to use instead, what may stay, scrubbing of recorded outputs, and a check before committing. |
| `docs/AGENTS.md` | One line under "Tooling and hygiene" pointing to that rule for the documents. |

## Single AGENTS.md (AGENTS v3)

| Change | Detail |
|---|---|
| `docs/AGENTS.md` removed | Its documentation rules moved unchanged into a "Documentation rules" section of the root `AGENTS.md`, so agents have one instruction file. Its hygiene lines were already covered by the root file (scratch files) or are now part of "Working here" (file names). |
| References | `design.md` header, `development-plan.md` task-status note, this file's introduction and the version table now point to the root file. Older entries above still name `docs/AGENTS.md` as it was then. |

## Project rename to Agent Relay (design v0.16)

| Change | Detail |
|---|---|
| Name | The project is now "Agent Relay" (distribution `agent-relay`), replacing "Agents Coordinator" (`agents-coordinator`). |
| CLI and package | The `orch` command and Python package became `relay` (`src/relay/`). Names derived from it changed too: the `refs/orch/` ref namespace, the `.orch-incoming` directory, the `ORCH_*` environment variables and the `orch-` temp prefixes now use `relay`. The role word "orchestrator" is unchanged. |
| Older entries | Entries above keep the old names as they were then. |

## Phase tag corrections (design v0.16)

Review finding R16-1 (low): two phase-4 features had no in-place phase tag, and three `v1`/`v2` labels conflicted with the phase numbering in section 16. Wording and clarification only; no rule changed, so the design version stays v0.16.

| Change | Detail |
|---|---|
| Section 5.3 | The `spawn_requests` field in the `result.spec.yaml` example carries a `# phase 4` comment. |
| Sections 8.4 and 8.5 | Headings tagged `(phase 4)`. |
| Section 13.1 | Surface 1 is marked `phase 3` instead of `v1`. |
| Section 13.2 | Surface 2 is marked `phase 5` instead of `v2`. |
| Section 17 | The open planner risk row says user-supplied graphs arrive in `phase 2a` instead of `v1`. |

## Antigravity CLI adapter (design v0.17)

| What changed | Where |
|---|---|
| Research finding: `agy` is the Antigravity CLI (a Go single binary and Google's terminal surface of the Antigravity 2.0 agent harness), the successor to Gemini CLI for unpaid-tier and Google One users since 2026-06-18, with legacy Gemini CLI remaining for some paid and enterprise plans | design section 7.2 |
| New prose block after the "Known caveats" paragraph maps the Antigravity CLI flags, JSON envelope, session resume, permissions and auth; the Gemini column of the 7.2 table is now described as legacy | design section 7.2 |
| Antigravity added to the per-adapter model flags, the native structured-output flag, the session-id field, and the auth and config-home environment handling | design sections 4.3, 5.4, 7.3, 9.4 |
| Antigravity/Gemini named in the roadmap, the risk register, and the references | design sections 16, 17, 18 |
| Task 4.1 retargeted in place to the Antigravity CLI (`agy`) adapter with legacy Gemini CLI behind it; no new task id | development-plan.md task 4.1 |
| Antigravity added to milestone M6, the critical-path sentence, the phase 4 intro and the compatibility-watch list | development-plan.md |
| Consistency pass: the adapter enum in the architecture diagram, the session-continuity pointer, and the usage source list now name Antigravity, and the section 17 forum-risk wording was corrected to the Sep 2026 forum recommendation; no rules changed | design sections 2, 6, 11, 17 |

Not yet verified and kept **[verify]** until a canary runs against a real install: the `--sandbox` containment scope and whether `--mode plan` prevents all repository mutation (so `workspace`/`readonly` stay unadvertised), a config-home environment override (none documented), thinking-token billing, and prompt delivery on argv (long-prompt canary). The unresolved third-party headless-launch question and the soft-deny-exits-0 behaviour are recorded as risks in section 17.

## Routing and adapter review decisions (design v0.18)

Wording and decision clarifications arising from implementing tasks 1.2, 1.3, 1.4, 1.9 and 1.10 and their review. No task ids were added or renumbered.

| What changed | Where |
|---|---|
| Score formula specified with concrete weights (a category-match term, an inert `strengths` term, a cost penalty, and recent-failure and running-attempt deductions); availability is now a hard filter applied before scoring rather than an additive term | design section 4.2 |
| A tier present in `models` but omitted from an explicit `escalate_on_failure` list is now outside the escalation path | design section 4.3 |
| `model_rule` gains an `escalation` value for tier moves; `include_goal: false` now emits a structured reason code (fixed `agent_rule`/`model_rule` fields plus optional status tokens) instead of free text; a minimum secret-pattern redaction list is specified; disabled projections are never written or repaired and must not be treated as current | design section 4.4 |
| Codex adapter does not pass `--ephemeral` (it breaks in-node resume), nor `--ignore-user-config`/`--ignore-rules` by default; `--skip-git-repo-check` is unnecessary in a git worktree | design section 7.2 |

## Parsing trust boundary and task status (design v0.19)

Review of the parsing and environment-building work in tasks 1.2, 1.3, 1.4, 1.8, 1.9, 1.10 and 1.11 exposed a trust boundary that the design now states explicitly, and the task-status table is updated to match. No task ids were added or renumbered.

| Id | Severity | Finding | Resolution |
|---|---|---|---|
| R19-1 | High | `validate_result` preserved agent-supplied `id` and `agent`, allowing an agent to spoof node identity | `id`/`agent` forced to the true node id and adapter id, plus tests (section 7.3) |
| R19-2 | Medium | A malformed agent-supplied `usage`, `model` or `session` could consume the single repair | Stripped before validation (section 7.3) |
| R19-3 | Medium | `Invocation.full_env` defaulted to fail-open, so a forgotten flag merged the parent environment | Code only: `build_env` now returns a marked complete environment; plain dicts still merge |
| R19-4 | Medium | Codex recorded the `default` tier sentinel as `result.model` | Null when the sentinel is unresolved (section 7.3) |
| R19-5 | Low | An unavailable substituted default model was not re-checked | Recheck added (section 4.3, v0.18) |
| R19-6 | Decision | A tier in `models` but omitted from an explicit `escalate_on_failure` list never escalates | Recorded in section 4.3 (v0.18) |
| R19-7 | Decision | Codex `--ephemeral` conflicts with in-node resume; the ignore flags are not passed by default | Recorded in section 7.2 (v0.18) |

| What changed | Where |
|---|---|
| Task-status rows added for 1.2, 1.3, 1.4, 1.8, 1.9, 1.10 and 1.11; 1.9 and 1.10 left `in progress` pending a live smoke run | development-plan.md |

## Worktree and capture reconciliation (design v0.20)

Reconciles the settled worktree-manager and change-capture decisions from the implemented code (tasks 1.14 and 1.15) into the design. No task ids were added or renumbered.

| What changed | Where |
|---|---|
| Windows `auto` worktree root uses `<repo-hash>/<run-short>/<node-short>`, where `<run-short>` and `<node-short>` are deterministic truncate-and-hash shortenings, not a literal first-8-character run id (which can collide) | design section 8.3 |
| Windows long-path handling enables the repo-local `extensions.worktreeConfig` switch and sets `core.longpaths` per worktree, so the main worktree is untouched beyond that one switch | design section 8.3 |
| Submodule init is scoped with `-c protocol.file.allow=always` and is a hard error on failure; Git LFS is best-effort only when `git-lfs` exists and `.gitattributes` declares `filter=lfs`, with absence or failure recorded | design section 8.3 |
| Cache isolation points `CARGO_TARGET_DIR` at `.agents/scratch/<run>/<node>/target`; credential-bearing homes (`CARGO_HOME`, `RUSTUP_HOME`, …) are passed through, never isolated | design section 8.3 |
| Decommission contract: `decommission` vs `decommission_candidate` entry points, refusal of the main worktree and unregistered/unrelated paths, caller-policy retention, and creation-failure cleanup | design section 6 |
| Change capture resolves `input_commit` to a canonical sha; the candidate is a detached worktree at a floating commit, and `candidate_tree` (not the floating commit) is the publication source | design section 8.3 |

## Verifier and permission reconciliation (design v0.21)

Reconciles the settled verifier and permission/doctor decisions from the implemented code (tasks 1.16, 1.20, 1.12) into the design. No task ids were added or renumbered.

| What changed | Where |
|---|---|
| Default verifier mode: explicit executable `RunCheck` acceptance entries win, otherwise the configured `build`/`test`/`lint` commands run in that order, skipping unset entries; a `diff_scope` entry never counts as executable; a write node with no executable check is rejected before dispatch (`NoExecutableChecksError`) | design section 10 |
| `verify: none` with a `verification_skip_reason` skips only command checks: the scope check is still enforced and a scope violation still fails; the outcome is `skipped` (recorded with its reason, never `passed`) and does not block publication | design section 10 |
| Effective scope is the union of all `ScopeCheck.diff_scope` entries, else `owns`, else the whole repository; rename/copy sources and destinations, deletions, binary files, and literal directory prefixes or globs are all checked | design section 10 |
| Command execution mechanics: platform-correct argv splitting (POSIX `shlex`, Windows `CommandLineToArgvW`), no shell, per-command `timeout_s` plus an optional overall timeout, `expect_output` as a stdout substring, candidate working directory, captured stdout/stderr/exit code | design section 10 |
| Candidate-unchanged: the candidate HEAD must not move and tracked files must not change (`git status --porcelain`, untracked artifacts ignored); a mutation fails verification and verification never edits the published tree | design section 10 |
| Permission precedence rejects unknown level values outright, so a misspelling cannot fall back to a default or wider level | design section 9.2 |
| Claude, Codex and Antigravity currently advertise `full` only; a passing canary never auto-promotes a profile into `probe().levels` (promotion is a manual adapter change after `enforced`) | design section 9.3 |
| Permission canaries classify `enforced|bypassed|inconclusive|unavailable|error` from filesystem evidence, never the exit code; a `readonly` canary ignores CLI-state writes outside the repository fixture; the result reports mapped flags and advertised status without changing `probe().levels` | design section 9.3 |
| Session-resume canary confirms resume only on exact token equality; a mismatched follow-up is `inconclusive`, not proof (the unverified-session risks stay in section 17) | design section 7.3 |
| Adapter canaries split by cost: `version` and `auth` are free, `structured`/`session`/`quoting` are paid and opt-in | design section 14 |

## Output publication reconciliation (design v0.22)

Reconciles the settled output-publication decisions from the implemented code (task 1.17, `src/relay/core/publish.py`) into the design. No task ids were added or renumbered.

| What changed | Where |
|---|---|
| Output commit authored/committed by the orchestrator identity with a deterministic message and pinned timestamps; the node's output is pointed to by the `refs/relay/<run>/<node>` ref (replacing "advances the node branch"); no-change nodes reuse the input commit; the diff artifact is written at the fixed path `artifacts/diff.patch` with a deterministic pinned diff configuration | design section 8.3 |
| Node refs: immutable per-attempt refs `refs/relay/<run>/<node>@<attempt>`, sibling (not descendant) retry outputs, idempotent same-attempt republish, refusal of same-attempt-different-content, compare-and-swap updates with rollback, and no backward movement of the current ref | design section 8.3 |
| Integration branch named `refs/heads/relay/<run>/integration`; successful outputs only, deterministic ascending node-id order, fast-forward/no-op/real-merge ancestry, conflict raises before any write, never moved backward, compare-and-swap, duplicate node ids rejected, phase 1 refuses a rebuild | design section 8.3 |
| Publication guards: refused unless verification allowed it, candidate-tree reserved runtime paths refused rather than stripped, ids validated before any ref write, `base`/`integration` reserved ref components, user's HEAD/branches never touched | design section 8.3 |
| Worktree branch discrepancy reconciled: write-node worktrees are detached in phase 1; a branch-backed node worktree is tagged phase 2 | design section 8.3 |
| Graph validation notes node ids may not be a reserved ref component | design section 8.1 |
| Durable-snapshots summary names the integration branch by its full ref | design section 6 |

## Route log reader reconciliation (design v0.23)

Reconciles the settled `relay routes` and `relay explain` behaviour from the implemented code (task 1.5, `src/relay/cli/commands/routes.py`) into the design. No task ids were added or renumbered.

| What changed | Where |
|---|---|
| Route-log readers documented as reading canonical `node.routed` events directly from each run's `events.jsonl`, never the projection, rather than only when a projection is missing | design section 4.4 |
| Documented the phase-1 limitation that the entry persists `alternatives` but not `busy`/`exhausted` candidate lists, which surface only through the structured reason code (`include_goal: false`); phase 2 may add structured fields | design section 4.4 |
| Observability pointer updated to state the direct-event read and defer to section 14 | design section 11 |
| Command surface expanded with the full `relay routes` flag set (`--model`, `--category`, `--order`) and `relay explain --json`, plus prose for conjunctive filters, deterministic `(ts, run_id, seq, attempt)` ordering, `--tail`, the JSON output shapes, error behaviour, and `include_goal: false` printing | design section 14 |

## Run loop, apply and inspection reconciliation (design v0.24)

Reconciles the implemented foreground run loop (task 1.19 slices A and B1), git-mode `relay apply` (task 1.24) and the inspection and clean commands (task 1.22) into the design. No task ids were added or renumbered.

| What changed | Where |
|---|---|
| `budget.max_attempts` stated as a total per-node limit with a phase-1 default of one attempt when unset; phase-1 retries pinned to the originally routed agent; every attempt emits the normal `routed` entry with its attempt number and there is no `retried` event; model escalation, cross-agent fallback and timeout policy tagged phase 2a | design section 8.2 |
| Git-mode apply reconciled with the implementation: prepared-tree verification uses the run's recorded integration acceptance (or graph defaults) and reports not run when none are recorded and skipped when the graph opted out with a reason; `--force` is rejected; `--branch` updates an existing branch or creates a missing one without switching the checkout; confirmation and non-TTY exit 7; the delta is installed into the current branch's index and working tree; phase 1 has no journal or durable operation id, relying on re-preparation and the expected-ref compare-and-swap | design section 8.3 |
| `apply.*` event names, fields, statuses and progress steps documented once in the catalog | design section 11 |
| Inspection commands documented as implemented: `status`, `result`, `agents` and `clean` flags, JSON shapes and exit behaviour, including `--purge-runs` confirmation on a non-TTY (exit 7), skipping live runs, and never creating, replacing or unlinking lock objects | design sections 11, 14 |
| Observability corrected: status renders the run record, event history and node table as implemented; cost reporting is tagged phase 4 | design section 11 |
| Task status updated for 1.19 (slices A and B1), 1.22 and 1.24 | development-plan.md |

## Implementation review corrections

Updated task status after implementation review and Windows/Linux validation against the existing design; no design rules changed.

| Finding | Severity | Resolution |
|---|---|---|
| CR1: staged edits lost during apply | P1 | Corrected baseline overlap handling against section 8.3; refreshed task 1.24 status. |
| CR2: Windows batch argument injection | P1 | Corrected shim escaping against section 7.2; refreshed task 1.7 status. |
| CR3: Windows worktree cleanup and retry failure | P1 | Corrected registration and long-path removal against sections 6 and 8.3; refreshed tasks 1.14 and 1.19 status. |
| CR4: surviving POSIX descendants after timeout | P1 | Corrected process-group escalation against sections 7.1 and 15; refreshed task 1.7 status. |
| CR5: inspection selected unfinalized file results | P2 | Aligned finalized spec placement with section 6; refreshed task 1.19 status. |
| CR6: separate-branch delivery rejected checkout edits | P2 | Corrected apply preparation against section 8.3; refreshed task 1.24 status. |
| CR7: platform-dependent checks failed | P2 | Corrected fixtures, canary argv and Windows process liveness against sections 6, 7.2, 9.4, 15 and 16; refreshed tasks 1.12, 1.16, 1.18 and 1.20 status. |

## Documentation reconciliation (design v0.25, plan v0.5)

Reviewed all repository Markdown guides against the CLI handlers, run pipeline and platform fixes.
This revision records local validation already performed; it does not claim a new live-agent or CI run.

| ID / severity | Finding | Resolution |
|---|---|---|
| D1 / P2 | Entry guides implied concurrency or a completed phase-0-only implementation. | Reworked the README and AGENTS entry points to use the design phase map and canonical task status; linked the plugin guide to its owning sections. |
| D2 / P1 | The request-only CLI could not supply the verification needed for a write run, while the reference implied the full surface was available. | Defined the intake boundary and intermediate input-result handling in design section 14; added plan task 1.31. |
| D3 / P2 | Phase-1 lifecycle and acceptance tasks still included deferred execution extensions. | Aligned plan tasks 1.18, 1.19, 1.25, 1.30 and 2.8 with design sections 6, 8.2 and 16; refreshed task status evidence. |
| D4 / P2 | Corrected storage, process and delivery behaviour lacked precise canonical wording. | Updated design sections 6, 7.1, 7.2 and 8.3, with regression references in section 16. |
| D5 / P2 | The layout sketch and evidence guide could imply modules or live coverage beyond what the code provided. | Reconciled design section 15, the plugin guide and recorded-fixture README; separated capture provenance from the live-smoke procedure in section 16. |

## Single-node CLI verification intake (design v0.26)

| What changed | Where |
|---|---|
| Replaced the request-only boundary with task-file and explicit verification/limit intake, override validation, unsupported-input rejection and JSON output handling. | Design section 14 |
| Connected the intake's authorized verification policy to prepared-tree delivery and clarified prompt rendering for supplied task requirements. | Design sections 5.2, 14 and 8.3 |
| Added CI interpreter-resolution and cache policy, and refreshed remote validation evidence. | Design section 16; development-plan task 0.2 status |
| Recorded implementation work on CLI intake and hyphenated test-file discovery. | Development-plan task 1.31 status; design section 15 |

## Live-adapter and Python-matrix validation corrections

| ID / severity | Finding | Resolution |
|---|---|---|
| P0V1 / P1 | Native Windows CLI execution lost essential variables after environment names were copied into a plain mapping. | Clarified Windows environment-name handling in design section 9.4 and added regression coverage. |
| P0V2 / P2 | The IPC spike inspected a socket path after Python could remove it on server shutdown. | Moved permission observation into the listening lifetime and awaited shutdown; design section 15 remains unchanged. |
| P0V3 / P2 | A fake-worker timeout test could expire during cold interpreter startup. | Allowed startup time in the fixture budget while retaining timeout and partial-edit assertions; design section 16 remains unchanged. |
| P0V4 / P2 | The Claude opt-in smoke checked availability without exercising result parsing. | Extended it to a real structured reply and resumed-context assertion; recorded validation evidence in task 1.9. |

## Foreground clarification and reply intake (design v0.27)

| What changed | Where |
|---|---|
| Replaced the unsupported-input CLI boundary with foreground reply/resume intake, validation and confirmation, and specified the single-node resume boundary. | Design section 14 |
| Added cumulative active-duration storage and connected the existing input protocol to executable command and canary coverage. | Design sections 6, 8.2 and 14 |
| Recorded operator-requested deferral of CI/live Codex validation and continued the remaining P0 work. | Development-plan task status |

| ID / severity | Finding | Resolution |
|---|---|---|
| IN1 / P1 | Escaping the full native result schema could exceed the Windows batch command-line limit. | Removed nonfunctional schema titles for Claude and added a complete-schema shim canary; design section 5.4. |

## User walkthrough and remaining-work reconciliation (plan v0.7)

| ID / severity | Finding | Resolution |
|---|---|---|
| UG1 / P2 | The entry guide ended before a user could run a verified write task and deliver it. | Added the sibling-project walkthrough and inspection/apply commands to the root README. |
| UG2 / P2 | Foreground questions lacked user-facing examples for replies, answer files and explicit limit increases. | Added a reply/resume walkthrough and links to the canonical contracts. |
| UG3 / P2 | The source-layout sketch omitted the foreground answer module and the command overview omitted existing doctor options. | Reconciled wording in design sections 14 and 15; no behavior changed. |
| UG4 / P2 | Remaining work was spread across individual statuses without an order reflecting the requested deferrals. | Updated task 1.28 and added the remaining-work order inside the development plan's Task status section. |

## CLI error handling and foreground recovery (design v0.28, plan v0.8)

| ID / severity | Finding | Resolution |
|---|---|---|
| UX1 / P2 | Parser, unknown-check and phase errors did not consistently honor JSON mode, and handler exceptions could escape. | Defined the command error boundary in design section 14 and expanded the acceptance check for task 1.29. |
| UX2 / P1 | Foreground resume could answer questions but could not recover an interrupted attempt or explicitly retry a failed run. | Defined bounded whole-attempt recovery and process-identity safeguards in design sections 6 and 14 for task 1.23. |
| UX3 / P1 | Interruption could lose the question-set counter and make later questions reuse an old revision. | Persisted the counter and preserved prior answer/result revisions during recovery; design sections 6 and 8.2. |
| UX4 / P2 | Remaining-work ordering still listed completed implementation and mixed it with deferred validation. | Reconciled the development-plan Task status section and refreshed the README recovery examples. |

## Dependency and remaining-work review (plan v0.9)

| ID / severity | Finding | Resolution |
|---|---|---|
| DP1 / P2 | The milestone summary serialized CI before independent foundation implementation and overstated supervisor dependencies for later phases. | Replaced the summary with the dependencies and exit gates expressed by the task tables. |
| DP2 / P2 | The remaining-work section omitted optional work available during the validation deferral and could read as a strict sequence between independent checks. | Separated optional implementation, deferred MVP closure and post-M2 work; identified independent CI, adapter and walkthrough checks. |
| DP3 / P2 | The acceptance wording for 1.26 and 1.30 did not fully express their existing nightly-automation and final-sign-off scope. | Tightened those task rows without changing task IDs, priorities or phase scope. |
