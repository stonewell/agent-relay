# Revision changes

The history of changes to the project documents: version summaries, review findings and resolutions, and restructures. It is the only place that records *what changed and why*; the documents themselves state only the current design (`design.md`) and the current tasks (`development-plan.md`). Which file holds what is defined in `AGENTS.md`.

Section references (`section N`) in older entries point into `design.md` as it was at that revision, and may have moved since. The per-version regression scenarios are consolidated into the test plan in design.md section 16; the original per-version wording is kept here for traceability.

## Current versions

| Document | Version |
|---|---|
| `design.md` | v0.14 |
| `development-plan.md` | v0.2 |
| `AGENTS.md`, `docs/AGENTS.md` | v1 |

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

