# Revision changes

The history of changes to the project documents: version summaries, review findings and resolutions, and restructures. It is the only place that records *what changed and why*; the documents themselves state only the current design (`design.md`) and the current tasks (`development-plan.md`). Which file holds what is defined in the documentation rules of the root `AGENTS.md`.

Section references (`section N`) in older entries point into `design.md` as it was at that revision, and may have moved since. The per-version regression scenarios are consolidated into the test plan in design.md section 16; the original per-version wording is kept here for traceability.

## Current versions

| Document | Version |
|---|---|
| `design.md` | v0.35 |
| `development-plan.md` | v0.17 |
| `AGENTS.md` | v4 |

## Version summaries

v0.35 / plan v0.17: independent-review follow-ups to 2.29 (TU19–TU22): headless `apply` refuses while the run's owner is alive or uncheckable and runners publish `done` only after `run.finished`; purges never delete a run branch a kept worktree has checked out; coordinator cancellation is bounded and its bookkeeping cannot replace the cancellation. Cursor follow-ups to 4.2/4.11 (CU2–CU5): shared command resolution, a version check in `probe()`, `--trust` at `full` only, and refusal of unadvertised levels. Details are recorded below.

v0.34 / plan v0.16: Windows acceptance and hardening of phase 2c (task 2.29): observer paths no longer sweep temporary files, coordinator turns are cancel-safe, and `relay clean` uses the shared purge rules; 2.29 is done for Windows and Linux/macOS stays with 2.33. The same version records the opt-in Cursor adapter (task 4.2) and its first doctor canaries (task 4.11). Details are recorded below.

v0.33 / plan v0.15: Textual TUI over the phase-2c core (task 2.28); the 2.28 status row replaces its remaining-work entry. Details are recorded below.

v0.32 / plan v0.14: shared cumulative-session delivery service (task 2.27); the 2.27 status row replaces its remaining-work entry. The table above had not followed the v0.31 foundations entry and now shows the current versions. Details are recorded below.

v0.30 / plan v0.12: added the phase-2c foreground interactive coordinator and Textual TUI, with milestone M8 and implementation tasks 2.20–2.29; narrowed task 5.4 to the advanced provider-backed planner extension. Details are recorded below.

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

## Foreground graph implementation priority (plan v0.10)

| What changed | Where |
|---|---|
| Moved deferred MVP closure behind foreground graph implementation at operator request and lowered its remaining P0 tasks to P1. | Development-plan task tables and Remaining work in order |
| Replaced task 2.1's release-milestone dependency with completed implementation prerequisites, while retaining MVP closure for graph milestone sign-off. | Development-plan milestone dependencies, task 2.1 and M3 exit criteria |
| Moved optional dry-run work behind the active graph queue and preserved existing validation deferrals. | Development-plan Remaining work in order |

## Foreground graph coordination (design v0.29, plan v0.11)

| What changed | Where |
|---|---|
| Clarified graph depth, portable IDs, priority and worker pinning, and corrected the example depth bound. | Design section 8.1 |
| Specified foreground graph intake, effective setting inheritance, planning output, replies, resume limits and file-based cancellation; distinguished foreground ownership from the future supervisor. | Design sections 8.6 and 14 |
| Added graph validation, scheduling, reservations, dependency snapshots, verified merges and bounded conflict resolution, final integration, retry/fallback, reuse/invalidation and foreground commands. | Development-plan tasks 2.1–2.11; README graph walkthrough; wording reconciliations in design sections 14–15 and single-node CLI intake |
| Specified adapter execution fingerprints for safe completed-node reuse, the initial Claude capability boundary, and retained-output behavior when reuse cannot be established. | Design sections 6 and 7.2; section 14 cross-reference |
| Reconciled phase-2a task completion and current validation evidence after graph closure. | Development-plan Task status |
| Document review: made the `readonly` profile for read-access nodes release-blocking (spike 0.12 raised to P1, new task 2.30); added coordinator-profile spike 2.31 and adapter capability task 2.32 ahead of 2.24; clarified that dependencies mean implemented and validation gates are separate; fixed priority/dependency inversions (2.3, 3.2, 5.3); extended security review to M8; removed run-id reuse, resume-privilege, `large-context` and stale phase-2 wording contradictions. | Design sections 3, 6, 8.3, 8.5, 8.6, 9.3, 15, 17; development-plan phases 0, 2a, 2c, 3, 5, risks and remaining work |

| ID / severity | Finding | Resolution |
|---|---|---|
| GR1 / P1 | Concurrent Windows worktree setup could contend on shared Git configuration. | Serialized worktree creation while retaining concurrent worker execution; covered by the two-writer integration test. |
| GR2 / P1 | Re-merging outputs already contained in a resolved descendant could repeat a conflict. | Removed ancestor inputs before deterministic snapshot merges and added retained-ancestry coverage. |
| GR3 / P1 | Persisting omitted node defaults could change verification inheritance on resume. | Stored effective graph defaults and tested the public run/resume path. |
| GR4 / P1 | Resumes left capture/merge scratch registered, and repeated blocked resumes could bypass orphan-worker checks. | Added scratch cleanup after termination proof and per-node recovery admission across repeated resumes (section 6). |
| GR5 / P1 | Cancellation could release ownership while the underlying worker thread remained active, including during Runner-wide cancellation. | Held cancellation ownership until the worker thread completed, including Runner-wide cancellation (sections 6 and 8.2). |
| GR6 / P2 | Replies for pending question nodes could be rejected in quiescent graphs with unrelated failed nodes. | Admitted valid pending-question replies in quiescent mixed-failure graphs (sections 8.2 and 14). |
| GR7 / P2 | Unsupported implicit or terminal node-cancellation requests could be silently ignored. | Rejected unsupported implicit and terminal node-cancellation requests (section 14). |
| GR8 / P1 | Registry-wide eligibility disabled production reuse; cached resolvers bypassed fingerprint/ref/hash checks, and cache misses could remove output refs before admitting an attempt. | Added the actual-adapter fingerprint capability, resolver integrity checks, and an admission guard protecting refs until an attempt was admitted (section 6). |
| GR9 / P2 | Completed checkouts remained registered when interrupted runs reused a validated output. | Added decommissioning after validated hits with durability-preservation regressions (section 6). |
| GR10 / P1 | Fallback or restored workers could miss the effective per-agent or CLI permission ceilings. | Resolved ceilings for each admitted actual agent and persisted a copied effective configuration for continuation (section 9.2). |
| GR11 / P2 | Clarification could trigger failure escalation, while split retries could lose the selected route, model and attempt facts. | Separated failure retries from answer continuation and preserved the actual routing decision, history, attempt facts and overrides (sections 4.3, 4.4 and 8.2). |
| GR12 / P1 | Inline answers could open a competing EventLog writer and duplicate canonical sequence numbers. | Shared the active writer for inline answers; quiescent callers closed only their own writer (sections 8.2 and 11). |

## Interactive foreground coordination (design v0.30, plan v0.12)

| What changed | Where |
|---|---|
| Added versioned conversation, coordination and handoff contracts; coordinator/progress capabilities; a session-owned foreground controller and Textual conversation/task/inspector UI. | Design sections 5.6, 6, 7.4, 8.7, 14.1 and 15 |
| Added the phase-2c roadmap, test areas, capability qualification and open risks for approved interactive rounds, checkpoints and cumulative delivery. | Design sections 9–11 and 16–18 |
| Added milestone M8 and phase-2c tasks 2.20–2.29, including the session-lock prerequisite and the separation of optional progress streaming from the P0 TUI; narrowed task 5.4 to the advanced provider-backed planner extension. | Development-plan milestones, phase 2c and tasks 2.20–2.29; task 5.4 |

## Phase 2c foundations (design v0.31, plan v0.13)

| What changed | Where |
|---|---|
| `readonly` is advertised per platform from canary evidence (Claude plan mode and Codex `-s read-only` enforced on Windows 11); `workspace` stays unadvertised; the Codex canary needed `--skip-git-repo-check`. | Design 9.3; plan 0.12, 2.30 |
| Coordinator contract is a versioned object whose `response` field is the closed action union, exported as `schemas/coordinator-response.json`. An empty qualification matrix keeps coordination unavailable. Spike 2.31 found a Claude tool-free candidate and rejected Codex `-s read-only` (it reads outside the workspace). | Design 5.6, 7.4; plan 2.20, 2.31, 2.32 |
| Sessions: permanent id reservation through the lock object, native-lock ownership, immutable plans and approvals, run id reserved inside the launch intent, linked-run guards in `RunLock`, `apply`, `cancel` and `clean --purge-runs`. `.agents/sessions/` is now a reserved runtime path and part of the exclude block. | Design 6; plan 2.21 |
| Controller: owner commands with durable ids, live answers while independent nodes run, durable pause and drain, shutdown that drains before releasing ownership. | Design 8.7; plan 2.22 |
| Context broker and manifests: pinned commits, hash-validated artifacts, budgets with explicit omissions, retained tamper-evident payloads. | Design 5.6, 9.4; plan 2.23 |
| Planning loop: durable bounded turns, one repair, duration drain, independent plan validation, context requests only through the broker. Plan digests now cover the explicit base. | Design 5.6, 8.7; plan 2.24 |
| Checkpoints: sealed dependency-closed manifests under `refs/relay-sessions/<sid>/checkpoints/<cid>`, sources retained under `.../checkpoint-sources/<cid>/<node>` (a ref cannot also be a directory), conflict manifests without a claimed base, append-only integration evidence gating delivery, superseded rounds blocked until restored, pinned checkpoint or integration bases for follow-ups. | Design 6, 8.7; plan 2.25 |
| Progress: bounded lossy channel with gap markers, sanitization and redaction, Claude and Codex normalizers, streaming output sink with bounded capture. Claude streaming is opt-in until qualified per version. | Design 7.4, 11; plan 2.26 |

| ID / severity | Finding | Resolution |
|---|---|---|
| PC1 / P1 | `.agents/sessions/` was documented as reserved runtime state but missing from the reserved paths and the exclude block, so a session dirtied the user's tree and blocked the next run. | Added it to `RESERVED_ROOTS`, the single source for path filters and the exclude block. |
| PC2 / P1 | Inline answers rewrote `run.json` outside the store lock and could lose a concurrent worker update. | Moved the limit adjustment into one locked `update_run`. |
| PC3 / P2 | `drain` could report idle before a just-dispatched worker was counted. | The scheduler publishes its running count after dispatch and after each completion. |
| PC4 / P2 | A checkpoint ref and its per-source refs collided in git's ref namespace. | Sources moved to `checkpoint-sources/`. |
| PC5 / P2 | The coordinator's inline `--json-schema` was rejected by the CLI because of the OpenAPI `discriminator` keyword. | The adapter's compact schema drops it; the exported schema keeps it. |

## Cumulative session delivery (design v0.32, plan v0.14)

| What changed | Where |
|---|---|
| One delivery service, `relay.core.delivery`, serves `relay apply <run>` and the future TUI: prepare, verify, revalidate and install through the same prepared tree. `prepare_apply` accepts an explicit source commit and baseline; verification and the working-tree install moved from the CLI into core. Headless runs keep their behaviour, events and messages. | Design 8.3, 10, 14; plan 2.27 |
| Session delivery policy: folded from the approved graphs of the rounds up to the delivered one; scopes (union) and checks only accumulate, later opt-outs are recorded skips, and the session record's copy must equal the fold. | Design 10; plan 2.27 |
| Carried contributions are revalidated before delivery (checkpoint deliverability, hashes, lineage and ancestry; integration branch and node output refs), failing closed with every problem listed. A sealed checkpoint can be delivered directly. | Design 8.7, 10; plan 2.27 |
| Confirmation binds to a prepared-operation digest that includes the session revision, policy, carried identities and checkpoint evidence. New session event `delivery.policy`. | Design 10, 11; plan 2.27 |

| ID / severity | Finding | Resolution |
|---|---|---|
| CD1 / P1 | `relay apply <run>` on a session round verified only the last round's own policy, so a shortened follow-up (narrower scope, `verify: none`) could deliver a tree that no longer met earlier mandated checks. | Session-linked runs resolve the cumulative policy and check the whole baseline-to-delivered delta; earlier checks and restrictions stay required. |
| CD2 / P2 | Dirty-edit recognition used the last round's retained base, which does not hold the user's original `--allow-dirty` snapshot after several rounds. | Session delivery passes the first round's base as the baseline. |

## Textual TUI (design v0.33, plan v0.15)

| What changed | Where |
|---|---|
| `relay.tui` (app, projection, widgets, screens) projects one in-process application service, `relay.core.session_app.SessionApp`, which composes the session owner, `PlanningEngine`, `SessionController` and the shared delivery service. The service runs on its own event-loop thread (`ServiceHost`); Textual workers only await it, and application close drains execution before ownership is released. | Design 14.1, 15; plan 2.28 |
| Entry points: bare `relay` on a terminal, `relay tui`, `relay -C PATH tui`, `relay tui --session S` and `relay resume --session S` (exclusive with a run). A non-terminal `relay tui` is a usage error (exit 2); bare non-terminal `relay` keeps its usage error. Textual is imported only inside the `tui` launch. Textual (`>=8.0,<9`) is a runtime dependency. | Design 14, 14.1, 15; plan 2.28 |
| Core additions for the command surface: `SessionController.continue_round` (explicit continuation of an interrupted, not superseded round), `planning.recover_turns` (turn recovery without a coordinator), `PlanningEngine.plan_base` (proposals against a checkpoint or unapplied integration, covered by the plan digest) and `sessions.delete_session` (confirmed deletion with a durable marker). | Design 6, 8.7; plan 2.28 |
| The service finishes a settled round itself; coordinator review, continuing a round or turn, and restoring a superseded round are explicit. Approval of a plan proposed by a coordinator whose profile has since changed is refused. Checkpoint-and-revise runs the round's integration checks on the combined commit so the checkpoint can be carried into cumulative delivery. | Design 8.7, 14.1; plan 2.28 |

| ID / severity | Finding | Resolution |
|---|---|---|
| TU1 / P1 | `SessionOwner.acquire` swept the session directory's temporary files before taking the lock, so a second TUI opening a busy session could delete the live owner's in-flight atomic write. | Sweeping happens only after the lock is held. |
| TU2 / P1 | `RunStore.open` sweeps temporary files, so projections that opened the active run on every refresh broke the writer's atomic `run.json` update (observed as a failed node). | The service reads runs through a non-sweeping handle. Other observer paths that call `RunStore.open` or `SessionStore.open` on an active run (for example `linked_session`, `round_state`, `protected_run_ids`) still sweep; they are left for review. (Resolved in v0.34 as TU14.) |
| TU3 / P2 | A read-only `git merge-base` started from the projection while workers were being spawned on Windows intermittently never returned in the Pilot walkthroughs (its output pipes stayed open). | Projection git queries use no stdin, a 30-second bound and a short cache; core `apply._run` now also uses no stdin and a 900-second bound (a timeout is a `GitApplyError`). The root cause was not isolated. |
| TU4 / P2 | A quit-time shutdown and the host's final shutdown could overlap, so the second returned while the first was still draining and the loop stopped under it. | Shutdown is one shared operation and release is serialized. |
| TU5 / P2 | After an owner crash `run.json` stays `running`, so `continue_round` refused the round and the TUI hid Continue. | A `running` round is continued when its run-lock owner is stale or absent; run recovery still proves old workers ended. The projection offers Continue for it. |
| TU6 / P2 | Quitting during Apply could release ownership while the install thread still ran. | Apply is a tracked service operation; stop and shutdown wait for it, session switching refuses it, and quit counts it as busy. |
| TU7 / P2 | Opening or deleting a session ignored a running coordinator turn, and release never cancelled it, so recovery could later block the turn on a live call. | Open and delete refuse while a turn runs, release cancels and drains a turn first, and deletion refuses active or blocked turns and `started` calls. |
| TU8 / P2 | `delete_session` swept temporary files before holding the lock, and a refused deletion still ran recovery (state and revision changed) and left the event log open. | Deletion checks existence without sweeping, inspects through a no-recovery acquire, and closes the event log on every path. |
| TU9 / P3 | Plan approval in the TUI trusted a turn-record proposer, failed open when it was missing and compared only agent and digest. | `save_plan` records the full proposing profile inside the plan digest; approval refuses a missing or different profile. |
| TU10 / P3 | Coordinator answers and turn resumption were accepted while a round executed, and coordinator context reads opened live runs with a sweeping `RunStore.open`. | Both are refused while executing (the composer offers only node answers then), and context reads use a non-sweeping handle. |
| TU11 / P3 | Stop and quit could race (stop reacquiring after shutdown released); a failed cancellation skipped release; the host could stop its loop under a running controller. | Stop reacquires under the release lock and not once closing; release always runs; `ServiceHost.close` waits while `still_running` reports work. |
| TU12 / P3 | A crash during deletion could strand records hidden from the session list. | `session.json` is removed last, so a partial deletion stays listed as deleting until finished. |
| TU13 / P2 | Runs kept after deleting their session could not be applied or resumed headless, because `RunLock`, `session_guard` and `prepare_run_delivery` require the session. | Operator decision: deleting a session also deletes its runs (`core.run_purge`, the `clean --purge-runs` rules). Live or unverifiable runs and runs shared with another session refuse; undelivered rounds, or rounds whose applied state is unknown, need an explicit confirmation that lists them; the marker lists the runs so a crash resumes. User branches are never touched. |

## Windows acceptance and hardening (design v0.34, plan v0.16)

| What changed | Where |
|---|---|
| `RunStore.open` and `SessionStore.open` no longer sweep temporary files. Only owners sweep: `SessionOwner.acquire` after the session lock is held (as before) and `RunLock.acquire` after the run lock is held. Every reader, including modules that still call `open`, is therefore an observer that changes nothing. | Design 6; plan 2.29 |
| Headless `relay apply` opens the run's event log on its first event, inside the session guard and after preparation found the run finished, instead of before any check. | Design 6, 14; plan 2.29 |
| Coordinator turns are cancel-safe: every durable write of a turn completes before a cancellation is delivered, the call's `try` starts before its call record is written, and a cancelled turn ends `interrupted` (an unexpected error ends it `failed`). | Design 8.7; plan 2.29 |
| `relay clean` plans and executes through `relay.core.run_purge`, the same rules confirmed session deletion uses. Its arguments, JSON fields and the exit codes of its successful, skipped, refused and approval-required outcomes are unchanged; only the failure cases of TU18 changed. | Design 6, 14; plan 2.29 |
| Windows acceptance evidence: offline suite and Textual Pilot walkthroughs, plus a Pilot check that Ctrl+C (a key in a Windows console) neither quits nor stops a round. Live coordinator/worker canaries stay opt-in and were not run. | Design 16; plan 2.29 |

| ID / severity | Finding | Resolution |
|---|---|---|
| TU14 / P1 | Follow-up to TU2: `linked_session`, `round_state`, `protected_run_ids`, a refused `RunLock.acquire` contender, headless `apply` (through `core.apply` and `core.delivery`), `clean` and session checks all reached `RunStore.open` or `SessionStore.open`, which swept a live owner's in-flight `.tmp-*` writes. | Opening never sweeps; the lock holder sweeps once. `tests/test_observers.py` holds a live session owner and run lock with in-flight temp files and shows every observer call and headless command leaves them; the same tests fail against the previous code. |
| TU15 / P2 | Headless `apply` opened the run's `EventLog` before the session guard and before knowing the run was finished. Opening repairs a torn tail, by truncating it or appending a newline, which corrupts a record a live writer is appending. | The log opens on first use, after the guard and after a running run has been refused; a refused `apply` on a live run leaves `events.jsonl` byte-identical. |
| TU16 / P1 | `PlanningEngine._invoke` wrote the call record, the turn and `coordinator.started` before its `try`, so a cancellation there left the call `started` and the turn `active`; deletion then refused until the session was reopened. A cancellation during any `asyncio.to_thread` write could also let that write land after the `interrupted` record, or after ownership was released. | Writes go through a helper that delivers a cancellation only after the thread finishes; the `try` covers the call record; a cancelled turn ends `interrupted` at every durable step (turn start, context selection, call record, turn record, `coordinator.started`). A quit inside the gap leaves no deletion problem and the turn resumes with its budget. |
| TU17 / P3 | An unexpected coordinator error (for example a profile that stopped matching between selection and call) left the turn `active` and the call `started` until a reopen. | The call and the turn end `failed` with the error, which still propagates to the caller. |
| TU18 / P2 | `relay clean --purge-runs` kept its own copy of the purge rules. Unlike its comment, it deleted the run record even when deleting one of its refs had failed, its git calls had no stdin redirection or time bound, and a failed worktree or ref listing read as "nothing to remove". | `clean` uses `core.run_purge`: the record is kept (with `run_directory.error`) and the run reports an error (exit 1) when an earlier removal failed, git runs without stdin and with a bound, and a listing failure is reported (exit 2) rather than ignored. Session deletion attempts every removal of a run before reporting failures, and still keeps the record. |

## Cursor adapter (design v0.34, plan v0.16)

| What changed | Where |
|---|---|
| `relay.adapters.cursor.CursorAdapter` for `agent` 2026.10.22-27dcf5f: `-p --output-format json --trust` with the prompt (and the result schema, since no schema flag exists) on stdin, the JSON reply parsed from the envelope's `result`, `--resume <session_id>`, `CURSOR_API_KEY` or the account login, and the legacy `cursor-agent` alias when `agent` is absent. `full` maps to `--force`; `readonly` to `--mode plan`, mapped but not advertised; `workspace` fails closed. Reasoning effort is not mapped. Cursor stays opt-in, outside the default foreground registry, and is neither a phase-2c coordinator nor a TUI worker. | Design 4.3, 7.2, 7.3, 9.3; plan 4.2 |
| Windows spawning: relay's outer `cmd.exe` disables delayed expansion, but Cursor's `agent.cmd` re-enables it and forwards `%*` through PowerShell 5.1, which drops double quotes and `!...!` spans from argv prose. The prompt therefore goes on stdin, which a shim-chain test shows is byte-exact; only short, character-checked tokens go on argv. | Design 7.2; plan 4.2 |
| Doctor: `adapter.cursor.{version,auth,structured,session,quoting,needs_input}` and `permission.cursor.{readonly,workspace}`, present only with `agents.cursor.enabled: true` and bounded at 300 seconds per model call. One live run each on Windows 11 with an account login is recorded in `tests/fixtures/recorded/cursor_doctor_canaries.json`: the six adapter canaries passed, both permission canaries were `inconclusive` (`--sandbox enabled` is refused by the Windows CLI, which has no filesystem sandbox). | Design 9.3, 14; plan 4.11 |
| Shared doctor change: the `needs_input` canary now validates a reply with each adapter's own result rule (nulls and empty `options` on free questions normalised, node id set by the orchestrator), which is slightly more lenient for Claude and Codex than the previous plain model validation; a passing session canary also reports `same_session_id` and `observed_fields`; canary specs and permission profiles carry a per-adapter call bound. | Design 7.3, 14; plan 4.11 |

| ID / severity | Finding | Resolution |
|---|---|---|
| CU1 / P3 | An `inconclusive` permission canary records only its classification, so the reason (for example the Windows CLI refusing `--sandbox enabled`) is not visible in the result. | Open follow-up under 4.11: keep the CLI exit code and a redacted first stderr line in the canary result. |

## Review follow-ups to phase 2c hardening (design v0.35, plan v0.17)

| What changed | Where |
|---|---|
| Headless `relay apply` (a run with no session link) checks the run lock's owner up front for a `done` run and again immediately before it first opens the event log; an `ALIVE` or `UNKNOWN` owner is refused with exit 1 and nothing changed. A run that is not `done` keeps its exit 2 refusal from preparation, which never opens the log. `relay clean --json` run entries gain `blocked_refs` (TU21). Single-node and graph runners append `run.finished` first and then write `state: done` and `final_commit` in one `run.json` update (the recovered-node finish does the same), so `done` is never visible before the last event. | Design 6, 14, 16; plan 2.29 |
| `core.run_purge` marks a run branch as blocked when a worktree the purge keeps has it checked out (the main worktree included): the plan lists it, it is not deleted, and the run record is kept. Checkouts are listed again after the worktree removals, so the branch of a worktree whose removal failed is kept too. `relay clean` shows blocked refs in its text plan and confirmation and in a new `blocked_refs` JSON field (`ref`, `worktree`); session deletion lists them as problems and refuses before its marker is written. | Design 6, 14, 16; plan 2.29 |
| A cancelled coordinator call is drained for at most `planning.CANCEL_GRACE_S` (10 s, also the existing drain bound after a duration limit), then its task is cancelled and the call record notes `cancel_forced`. The interrupted call record is written once; it, the turn record and `coordinator.finished` are each attempted, and a failure becomes a note on the propagating `CancelledError`. | Design 8.7, 16; plan 2.29 |

| ID / severity | Finding | Resolution |
|---|---|---|
| TU19 / P2 | Pre-existing. Headless `relay apply` could open a run's `EventLog` while the run's owner was still alive. Runners wrote `state: done`, then `final_commit`, then appended `run.finished`, all under the run lock, and `_prepare_headless` never checked the lock. So `apply` could append with a duplicate `seq` (and later opens failed with `EventLogCorrupt`), or repair as torn a tail that was really the runner's half-written record. | `apply` refuses a live or uncheckable owner before the log is touched (`events.jsonl` stays byte-identical). Runners publish `done` with `final_commit` in one write after `run.finished` is durable. A crash between the two leaves the run `running`; resume finishes it without redispatch (the killed-owner recovery test now crashes at that point). Records with `done` and no `final_commit`, written by older code, are still recovered. |
| TU20 / P3 | In `PlanningEngine._invoke`, the `finally` wrote the call record and `coordinator.finished` without suppression, so an `OSError` or `SessionError` there replaced an in-flight `CancelledError`. The interrupted call record was also written twice. | A dedicated close step writes the call record once, then the interrupted turn, then the event, and attaches each failure to the cancellation as a note. Cancellation always propagates. |
| TU21 / P3 | The `run_purge` docstring promised that the user's checked-out branch is never touched, but `purge_run` ran `git update-ref -d` on every `refs/heads/relay/<run>/...` ref, including one checked out in the main worktree or in a worktree whose removal had just failed. | Such branches are blocked, reported and kept, together with the run record, by both `relay clean --purge-runs` and session deletion. The plan output shows them before confirmation. |
| TU22 / P3 | `planning._drain` ignored repeated cancellation and never cancelled the call task, so stop and shutdown hung on a coordinator that ignored `cancel_processes_when`. | The drain is bounded by the grace period; then the task is cancelled and waited for once more with the same bound, and the forced cancel is recorded on the call. Tested with a fake coordinator that ignores the signal. |

## Cursor adapter follow-ups (design v0.35, plan v0.17)

| What changed | Where |
|---|---|
| The adapter and `relay doctor` resolve Cursor's command with one function, `resolve_cursor_command`: legacy `cursor-agent` is probed only when the configured `cmd` is the built-in `agent` and that is not on PATH, and an explicitly configured `cmd` is never replaced. | Design 7.2; plan 4.11 |
| `probe()` reports Cursor available only when `--version` parses as a Cursor version (for example `2026.10.22-27dcf5f`). A generic `agent` binary with other output is unavailable, with a note. | Design 7.2; plan 4.2 |
| `--trust` is passed at `full` only. It writes a persistent per-directory workspace-trust marker and skips the "Workspace Trust Required" prompt. Trust gates project code such as `.cursor/worktrees.json` setup and, interactively, also offers to enable project MCP servers; whether project hooks or rules depend on trust is not confirmed. `readonly` never passes it, so a headless `readonly` run in an untrusted worktree stops at the trust gate (fails closed). Doctor canaries still pass `--trust`, because they run in disposable directories. | Design 7.2, 9.3; plan 4.2, 4.11 |
| `build_invocation` refuses any level outside the adapter's advertised levels, so `readonly` (`--mode plan`) is unreachable until it is promoted into `probe().levels`. | Design 7.2, 9.3; plan 4.2 |

| ID / severity | Finding | Resolution |
|---|---|---|
| CU2 / P3 | Doctor probed the configured Cursor `cmd` literally, while the adapter fell back to `cursor-agent`, so the two could disagree about which binary was checked. | Both use `resolve_cursor_command`. |
| CU3 / P3 | `agent` is a generic command name, but `probe()` only noted an unrecognised `--version` output and still reported the agent available. | A non-Cursor version makes the probe unavailable, with the output in its note. |
| CU4 / P3 | `--trust` was passed for every level, so a `readonly` run would trust (and so run project code from) an untrusted repository. | `--trust` is passed at `full` only; `readonly` fails closed at the trust gate. |
| CU5 / P3 | `build_invocation` built a `--mode plan` invocation for `readonly` even though `readonly` is mapped but not advertised. | Unadvertised levels raise `PermissionUnsupported`. |

## Removal of agent-CLI verification (2026-10-29)

| What changed | Where |
|---|---|
| All agent-CLI verification is removed: qualification records and profiles, configuration digests, pinned CLI version/platform/auth mode, `inspect()` gating, `probe()` and `--version` checks, permission/adapter/coordinator canaries, `advertised_levels`/`READONLY_VERIFIED_PLATFORMS`, and the doctor's agent CLI checks. An agent is usable when it is `enabled` and has a gateway adapter; relay never interrogates the CLI and invokes it only when work runs. `relay doctor` now runs only `python`, `git`, `config`, `git.exclude` and `session.locks`. Rationale: relay must run whatever CLI the operator registers and leave CLI-level problems to the operator; the gates made correctly configured agents appear to have no models and required re-review whenever a CLI changed. | Design 7.2, 9.3, 14; plan 2.32, 4.11 |
| `GET /v1/models` lists every configured model alias with `null` limits until observed, plus `relay/auto` when defaults exist. `agy` models are enumerated from `agy models` (TSV `<slug>\t<name>`, cached per process, falling back to `agents.agy.models` then `("default",)`). `relay agents` and `opencode-config` likewise report configuration without invoking a binary. | Design 13.2; plan 5.0–5.2 |
| A missing, broken or misconfigured CLI fails at invocation with the command, exit code and a redacted stderr tail, on stderr and in the OpenAI-shaped HTTP error body. Kept: `--key` bearer auth on `/v1/*`, Host/Origin checks, process launching and environment allowlists. | Design 13.2, 14 |
| Tests: verification-only tests and fixtures are deleted, and expectations updated for configuration-based availability and listing, `agy models` enumeration, and detailed launch errors. | `tests/` |
