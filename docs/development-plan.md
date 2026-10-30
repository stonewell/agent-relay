# Development plan

Status: Working plan · Tracks the current `design.md`

This plan splits the phases in design section 16 into tasks, orders them by priority, and defines exit criteria per phase. It does not restate design rules; each task points to the design section that defines its behaviour. Where a task conflicts with the design, the design wins and this plan is corrected.

**Status (2026-10-29):** agent-CLI verification has been removed. Relay no longer records qualification profiles or digests, pins CLI versions/platform/auth mode, runs `probe()`/`--version` checks, permission/adapter/coordinator canaries or `relay doctor` agent `cli` checks. An agent is usable when it is `enabled` and has a gateway adapter (`claude`, `agy`); Relay invokes the CLI when work runs and reports its own failure (command, exit code, redacted stderr tail). `relay doctor` runs only `python`, `git`, `config`, `git.exclude` and `session.locks`, and `relay agents` prints configuration only. Permission levels are advertised as `full` and `readonly`; `workspace` is not advertised. Coordinator selection is requested → `claude` → first sorted, with no qualification record. Task definitions for completed work are retained in [completed-tasks.md](completed-tasks.md); remaining entries below that describe removed mechanisms are marked superseded in their sections.

## How to read this plan

**Priority**

| Priority | Meaning | Rule |
|---|---|---|
| **P0** | Critical path. Blocks other tasks or the phase exit. | Start first, staff first, never cut. |
| **P1** | Required for the phase exit, but not blocking other tasks. | Schedule after P0 in the same phase. |
| **P2** | Valuable, but the phase can exit without it. | First to cut or defer when schedule slips (see "Cut order" per phase). |

**Size** (rough, one engineer, includes tests): **S** up to 1 day, **M** 2 to 4 days, **L** 1 to 2 weeks. Sizes are for sequencing, not commitments; re-estimate at the start of each phase.

**Task ids** are `<phase>.<number>`. "Depends" lists task ids that must be implemented and passing their offline tests; live-agent and remote-CI validation gates are tracked separately in the status table and in "Lower-priority MVP release closure", and block milestone sign-off, not downstream implementation. "Done when" is the acceptance check; every task also needs its tests from design section 16 green and the design doc updated if behaviour changed.

**Workstreams** used for parallelism: **CORE** (config, specs, store, events), **ROUTE** (classifier, routers, route log), **ADAPT** (CLI adapters, spawning, environment), **GIT** (worktrees, capture, verify, apply), **RUN** (run lifecycle, locks, scheduler, supervisor), **UX** (CLI, plugin, docs), **QA** (test harness, canaries, CI).

## Milestones

| Milestone | Reached when | Phase |
|---|---|---|
| **M0 Foundations** | Schemas, config loader, store, events, fake adapter, CI on Windows and Linux; spikes reported | 0 |
| **M1 First routed run** | `relay run` routes a task, runs the fake adapter in a worktree, verifies and writes the route log | 1 (mid) |
| **M2 MVP** | Real Claude and Codex single-node runs, `needs_input`, `relay apply`, all phase 1 exit criteria | 1 |
| **M3 Graphs** | Foreground task graphs with merge, fallback, resume | 2a |
| **M4 Background** | Detached supervisor with native lock, IPC, crash recovery | 2b |
| **M5 Plugin** | OpenCode plugin driving the supervisor | 3 |
| **M6 Breadth** | Antigravity/Gemini, Cursor, cost accounting, shadow-git | 4 |
| **M7 Gateway** | Provider gateway, completion discovery and pinned-client acceptance; provider-backed planning is optional | 5 |
| **M8 Interactive** | Approved foreground CLI coordination, session-owned graph rounds, checkpoints and cumulative delivery with configuration-selected coordinators | 2c |

Task dependencies in the phase tables define implementation order. CI task 0.2 is an M0/M2 exit gate, rather than a prerequisite for independent schema and core implementation. M2 sign-off in 1.30 also requires every phase-1 P0/P1 exit check; completed downstream implementation does not close an outstanding adapter or CI validation gate.

Phase 2a implementation may proceed before M2 using the completed implementation prerequisites in its task table. Outstanding MVP validation is lower-priority release closure, not a blocker for foreground graph implementation. M2 remains required for M3 sign-off. Phase 2c implementation may start when its completed 2a prerequisites are met; it does not wait for M2 release sign-off or phase 2b. M8 release sign-off requires M3 and all phase-2c P0/P1 tasks. Phase 2b and phase 3 retain their M3/M4 dependencies below; phase-4 and phase-5 tasks use their individual dependencies and do not all require the background supervisor.

---

## Phase 0: Foundations (milestone M0)

Goal: everything later phases build on, plus the spikes that de-risk phase 2 and the MVP. No real agent runs.

Status (2026-10-29): 0.8's probe registry and canary runner and 0.12's `workspace` advertising recommendation were removed with agent-CLI verification; `relay doctor` now runs only the five environment/configuration checks listed in the status note above. Spike 0.11's findings remain historical input to adapter behaviour.

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 0.2 | CI matrix: Windows and Linux (macOS best effort), Python 3.11 and the newest stable; cache; required checks | P1 | S | 0.1 | 16 | PR checks run all tests on both OSes |

**Exit criteria (M0):** CI green on Windows and Linux; every schema in design sections 4.4 and 5 has valid and invalid fixtures; config loader passes the config test area; spikes 0.10 and 0.11 reported with decisions.

**Cut order:** the `workspace` half of 0.12 first (Windows then lacks `workspace`; the `readonly` half is needed by 2.30 and is never cut), then 0.9 (can move into 1.x).

**Parallelism:** after 0.1, CORE (0.3 to 0.6) and QA (0.2, 0.7) run in parallel; spikes 0.10 and 0.11 run alongside by a second engineer.

---

## Phase 1: MVP, foreground single node (milestones M1, M2)

Goal: `relay run "<request>"` routes a task, runs Claude or Codex in an isolated worktree, captures and verifies the result, handles `needs_input`, and `relay apply` delivers it. Design section 16 lists what is explicitly out of scope; do not add it.

Status (2026-10-29): 1.7's `probe()`, the doctor canaries in 1.12, 1.20 and 1.21, and 1.20's `probe().levels` were removed with agent-CLI verification. Permission levels are advertised as `full` and `readonly` for every enabled agent, and availability comes from configuration.

### 1A. Routing and route log (ROUTE)

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 1.6 | `relay run --dry-run` and `previewed` entries (the routing preview is reused by 3.2) | P2 | S | 1.4 | 4.4 | Dry run writes `previewed` entries with `executed: false` |

### 1B. Adapters and process spawning (ADAPT)

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 1.10 | Codex adapter: `exec`, `--output-schema` (strict-form conversion, section 5.4), `-o`, sandbox flags, long-form bypass flag, session resume, JSONL parsing | P1 | L | 1.7, 1.8, 0.11 | 7.2, 7.3 | Golden-output tests; live smoke test passes |

### 1C. Worktrees, capture and verification (GIT)

Completed task definitions for this section are in the [archive](completed-tasks.md).

### 1D. Run lifecycle (RUN)

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 1.21 | `needs_input` foreground: adapter prompt and typed questions, TTY prompt, answer recording and provenance, `--non-interactive` blocked exit 6, `relay reply`, `relay resume` for one node, limits and idempotency | P1 | L | 1.19, 0.11 | 8.2, 5.3 | `needs_input` foreground test area green (the doctor canary was removed 2026-10-29) |

### 1E. Delivery (GIT)

Completed task definitions for this section are in the [archive](completed-tasks.md).

### 1F. Quality and release (QA, UX)

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 1.25 | Integration suite on a temp git repo with the stub CLI: full run, same-agent retry, verification failure and foreground input paths; phase-2 routing extensions belong to 2.8 | P1 | L | 1.19, 1.21, 1.31 | 16 | Suite runs in CI on both OSes |
| 1.26 | Live smoke tests (`RELAY_LIVE=1`) for Claude and Codex, run manually and nightly where credentials exist | P1 | S | 1.9, 1.10 | 16 | Documented procedure and nightly coverage implemented; reference smoke tests pass |
| 1.28 | User docs: quickstart, `agents.yaml` reference, command reference, troubleshooting, known limits (Windows `full` only, no graphs) | P1 | M | 1.24 | all | A new user completes a run and an apply from the docs alone |
| 1.30 | MVP acceptance run and sign-off: scripted scenarios on the reference machine (simple feature, bugfix with same-agent retry, ambiguous task with `needs_input`, failed verification, apply with dirty tree) | P1 | M | 1.24, 1.25, 1.27, 1.31 | 16 | All five scenarios and every phase-1 exit criterion pass (**M2**) |

**Exit criteria (M2):** all tasks P0 and P1 done; the five acceptance scenarios in 1.30 pass on Windows and Linux; the phase 1 test areas in design section 16 are green; known limits are documented.

**Priority order inside the phase** (suggested sequence): 1.7, 1.18, 1.13 and 1.1 start immediately (they do not depend on each other) -> 1.2/1.3/1.8 -> 1.14, 1.15 -> 1.4, 1.16 -> 1.17 -> 1.19 (M1) -> 1.9, 1.10, 1.11 -> 1.21, 1.20 -> 1.24, 1.31 -> 1.25, 1.27 -> 1.30.

**Cut order if schedule slips:** 1.6, 1.23 (resume after interruption can re-run the node by hand), 1.26 nightly automation, parts of 1.29, then `--branch` in 1.24. Never cut 1.20 fail-closed behaviour, 1.21, or 1.27.

---

## Phase 2a: Foreground graphs (milestone M3)

Goal: a task graph runs in the foreground with isolation, merging, fallback and resume. No background process yet.

Status (2026-10-29): 2.30 is superseded/removed. `readonly` is advertised without canary-based promotion and `READONLY_VERIFIED_PLATFORMS` no longer exists; `workspace` is not advertised. Task 2.30 no longer gates M3; the remaining 2a tasks are unaffected.

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 2.30 | ~~Qualified `readonly` profile for read-access nodes: promote a profile into `probe().levels` per OS for at least one agent (for example Claude plan mode, Codex `-s read-only`) after the section 9.3 canary returns `enforced`; routing reports the unmet level otherwise~~ (superseded/removed 2026-10-29; `readonly` is always advertised and `workspace` is not) | P0 | M | 0.12, 1.20 | 8.1, 9.3 | No promotion or canary work remains; `readonly` is advertised for every enabled agent |

## Phase 2b: Background supervisor (milestone M4)

Goal: detached per-run supervisor with correct ownership, IPC and crash recovery. Gated by spike 0.10; on platforms without a qualifying binding this sub-phase is skipped and the foreground mode remains.

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 2.12 | Native lifetime lock integration (`relay.core.platform.locks` from 0.10), lock objects outside purgeable dirs, non-inheritance, `doctor` filesystem check, target locks | P0 | L | 0.10, M3 | 6, 15 | Simultaneous cold start and stale-socket tests green |
| 2.13 | Supervisor process: detached start (hidden on Windows), readiness acknowledgement, scheduler hosting, redirected logs | P0 | L | 2.12, 2.2 | 8.6 | Run survives front-end exit |
| 2.14 | IPC transport: Unix socket and Windows named pipe, path-length fallback, endpoint file, observer token, protocol versioning | P0 | L | 2.13 | 8.6 | Attach, status and cancel over IPC on both OSes |
| 2.15 | Privileged command path: observer/privileged split, peer-process check against worker trees, operation digests, confirmation flow, `approval_required` result | P0 | M | 2.14 | 8.6, 9 | IPC test area green; no test treats `--yes` as human proof |
| 2.16 | Crash recovery: owner generations, reconcile events and `run.json`, terminate or prove termination of old workers, stale-attempt rejection, interrupted publication recovery | P0 | L | 2.13 | 6 | Owner-death tests in every attempt phase pass |
| 2.17 | Background `needs_input`: replies through IPC, dependents of a blocked node held, answers append-only with revisions, `wait` semantics | P1 | M | 2.14, 1.21 | 8.2, 8.6 | Background `needs_input` tests green |
| 2.18 | `wait`, `status` and `cancel` front-end behaviour on IPC; `relay resume --max-attempts` | P1 | M | 2.14 | 8.6, 14 | Wait expiry returns actual state without cancelling |
| 2.19 | Cross-platform reliability pass: Windows pipe ACLs, PID reuse, long socket paths, antivirus/locking surprises | P0 | M | 2.16 | 6, 8.6 | Phase 2b suite green on all supported OSes |

**Exit criteria (M3):** M2 signed off; foreground graphs pass the graph, merge, snapshot/resume and overlap test areas on Windows and Linux (Linux `readonly` and macOS follow-up is 2.33, P2). **Exit criteria (M4):** ownership and recovery test area (phase 2b part) green; a run started by one front-end process is inspected, answered and cancelled from another.

**Cut order:** 2.10 template breadth, 2.8 extras, then 2.18 polish. Never cut 2.15 or 2.16.

---

## Phase 2c: Interactive foreground coordination (milestone M8)

Goal: a Textual interface guides bounded CLI-backed coordination through approved plans, foreground worker rounds, checkpoints and cumulative delivery. This phase builds on 2a without requiring the phase-2b supervisor.

Status (2026-10-29): coordinator qualification records, exact-profile digests and coordinator canaries were removed. `core.coordinator` only selects among enabled agents (requested → `claude` → first sorted), and the recorded proposer is the agent name. The qualification parts of 2.20, 2.32 and 2.33 are superseded; 2.29's Linux/macOS acceptance items remain.

Phase 2c may start when its individual task prerequisites are complete, without waiting for M2 release sign-off or M4. M8 sign-off requires M3 and all phase-2c P0/P1 tasks. Each task adds its own tests; task 2.29 performs final cross-platform acceptance and hardening rather than deferring test authoring.

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 2.33 | Linux/macOS follow-up for phase 2c and M3: full offline and Pilot acceptance and a real-terminal smoke test of `relay tui` (qualification, `readonly` promotion and `READONLY_VERIFIED_PLATFORMS` removed 2026-10-29) | P2 | M | 2.29, 2.30 | 9.3, 16, 17 | Linux (and macOS where available) offline/Pilot suites pass; `relay tui` smoke test passes |

**Exit criteria (M8):** M3 is reached; all phase-2c P0/P1 task acceptance is complete. Live CLI/model checks remain opt-in and do not replace offline tests.

---

## Phase 3: OpenCode plugin (milestone M5)

Goal: OpenCode agents route and run work through the supervisor. Depends on M4.

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 3.1 | Plugin scaffold and supervisor client (start or attach, observer/privileged calls), version compatibility check | P0 | M | M4 | 13.3 | Plugin loads in the pinned OpenCode version |
| 3.2 | Read and routing tools: `route_task`, `route_log`, `task_status`, `task_result` | P0 | M | 3.1, 1.6 | 13.1 | Handlers pass against a mocked `client` |
| 3.3 | Execution tools: `run_agent_task`, `run_task_graph` with `wait: false` default and `wait_timeout_s` | P0 | M | 3.1 | 13.1, 8.6 | Non-blocking return; wait expiry returns actual state |
| 3.4 | Privileged tools: `task_input`, `task_apply`, `task_cancel` with the host approval flow and `approval_required` results | P0 | L | 3.1 | 13.1, 8.6 | Approval digest mismatch rejected; no model-provided boolean counts as approval |
| 3.5 | Companion assets: `router` agent, `cli-worker` subagent, commands `/route`, `/fanout`, `/runs`; install without overwriting user edits | P1 | M | 3.2 | 13.1 | Assets installed once, preserved on update |
| 3.6 | Event mirroring: progress via `client.app.log`, optional child sessions; abort mapping (explicit user abort cancels, wait expiry does not) | P1 | M | 3.3 | 13.1, 8.6 | Plugin reload does not cancel background runs |
| 3.7 | Packaging: npm package for the plugin (requires installed `relay`, version check at load), local-file install, docs, `relay doctor` plugin compatibility check | P1 | M | 3.5 | 13.3 | Install works from both routes |
| 3.8 | End-to-end manual test in OpenCode on Windows and Linux; fix list | P0 | M | 3.4, 3.6 | 16 | Scripted walkthrough passes |

**Exit criteria (M5):** all tool handlers tested against a mocked client; the manual end-to-end passes; documented install. **Cut order:** 3.6 child sessions, then 3.5 commands.

---

## Phase 4: Breadth and delivery modes (milestone M6)

Goal: more agents, spawning, cost control and non-git workspaces. Tasks are independent, so order by value. Antigravity/Gemini and Cursor adapters depend only on M2; the rest need M3 or M4 as noted.

Status (2026-10-29): canary-based permission verification and promotion were removed, so the canary/promotion parts of 4.1, 4.2, 4.10 and 4.11 are superseded. Every enabled agent advertises `full` and `readonly`; `workspace` is not advertised. The `agy` adapter is available to the gateway, which enumerates its aliases from `agy models`.

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 4.1 | Antigravity CLI (`agy`) adapter (canaries removed 2026-10-29), with legacy Gemini CLI behind the same adapter where installed (availability depends on plan) | P1 | M | M2 | 7.2, 7.3, 5.4, 9.4, 16 | Golden tests on the `agy` JSON envelope and `stream-json` result, structured output, and `conversation_id` resume; aliases are enumerated from `agy models`; legacy Gemini checked only when installed |
| 4.11 | Cursor adapter verification (live canary/`doctor` check parts removed 2026-10-29): pinned Cursor CLI (`agent` 2026.10.22-27dcf5f; session id and resume, structured output, `--mode plan`/`--force`, `.cmd` shim on Windows), recorded outputs | P1 | M | 4.2, 0.11 | 7.2, 7.3, 9.3 | Recorded-output and golden tests pass offline; Cursor is not offered as a phase-2c coordinator or TUI worker |
| 4.3 | `spawn_requests`: validation, depth and node limits, no recursion, fresh-nodes-only mutation, events | P1 | M | M3 | 8.4 | Spawn test area green |
| 4.4 | Pricing catalog and token normalization, settlement formula, `estimated`/`unknown` labels, doctor warnings | P0 | M | M3 | 8.5, 3 | Pricing tests green |
| 4.5 | Reservations and admission control, advisory mode, `budget_exhausted` blocking, `relay resume --raise-budget`, status reporting | P0 | L | 4.4, M4 | 8.5 | Budget tests green including restart idempotency |
| 4.6 | Hard cost mode feasibility: live check for Claude `--max-budget-usd` as a strict bound; enable only if proven | P2 | M | 4.5 | 8.5 | Decision recorded; otherwise `hard` stays rejected |
| 4.7 | Shadow-git mode: baseline snapshot with exclusions, isolated store, non-git prompt/auto-init | P1 | L | M2 | 1, 6 | Runtime state excluded from the baseline; non-git run completes |
| 4.8 | Shadow-git `apply`: three-way sync, conflict manifests, journal, crash recovery, target locks, symlink/junction checks | P1 | L | 4.7, M4 | 8.3 | Delivery test area (shadow-git part) green with crash injection |
| 4.9 | `relay replay` and richer `explain` | P2 | S | M2 | 11, 14 | Timeline renders from events |
| 4.10 | ~~Permission profiles for more platforms~~ (superseded 2026-10-29: `full` and `readonly` are advertised for every enabled agent; `workspace` is not advertised) | P1 | L | 0.12 | 9.3 | No separate platform permission qualification remains |

**Exit criteria (M6):** each task meets its done-when; phase 4 test areas green. Tasks may ship independently as minor releases. **Cut order:** 4.9, 4.6, 4.10 (platform by platform).

---

## Phase 5: Gateway and planning (milestone M7)

Gateway v1 implementation and initial offline validation are in place; the status table separates code from client release gates. v1 is a foreground, stateless service independent of phase-2b IPC and the plugin; it reuses the process layer. Enabled agents with a completion adapter (`claude`, `agy`) serve the catalog; Codex has no gateway adapter. Tasks 5.1c/5.1d are later enhancements, not implicit v1 promises.

Status (2026-10-29): completion qualification records, profile digests and the reviewed-profile gate were removed. Gateway backends are the enabled agents with adapters, and `GET /v1/models` lists every configured alias with `null` limits until observed. The qualification-gate wording in 5.0, 5.1a, 5.1 and 5.2 below is retained as history.

| ID | Task | Pri | Size | Depends | Design | Done when |
|---|---|---|---|---|---|---|
| 5.0 | Compatibility measurements: exact tool-free completion invocation, native output limits, structured output and streaming; growing transcripts with tool/result round-trips, observed cache counters and latency | P1 | M | 2.31, 2.32 | 13.2.3, 13.2.7, 13.2.10 | Written invocation/OS/auth/config evidence and documented limitations; no qualification-based startup gate; CLI/schema and pinned-client parameter gaps recorded; unknown cache counters remain unknown. Session resume stays deferred |
| 5.1a | `CompletionCapable` and completion types; Claude candidate invocation/parser; private scratch, stdin, complete environment, exact output transport; no task/run-store dependency | P1 | L | 5.0 | 13.2.1, 13.2.4, 13.2.7 | Fake/recorded tests cover text, usage, profile drift, bounded output and process cancellation/drain; opt-in live checks cover each invocation mode and read/write/tool/hook/MCP suppression |
| 5.1 | `relay serve`, optional FastAPI/uvicorn extra, loopback/token, authenticated model catalog, aliases/auto routing, plain-text chat/SSE, atomic admission, request limits, shutdown and pre/post-SSE errors | P1 | L | 5.1a | 13.2.1, 13.2.2, 13.2.3, 13.2.5, 13.2.6 | Pinned OpenAI SDK works with configured models; slash ids and explicit pinning pass; real HTTP/subprocess tests cover chunked limits, 429, slow clients, both disconnect modes, cancellation races, shutdown drainage and cleanup; reassembled content is exact with no duplicated/lost deltas; uninstalled extra leaves core commands usable |
| 5.1b | Client-owned tool calling: deterministic history, call/result validation, bounded schema subset, tool-choice constraints, envelope validation and one repair, buffered tool SSE, honest usage aggregation | P1 | L | 5.1 | 13.2.3, 13.2.5, 13.2.10 | Tests reject unknown/duplicate ids, invalid names/arguments and unsatisfied forced/required/parallel choices; unsupported schema features fail before launch; no invalid/partial tool calls escape; repair shares deadline/permit and aggregates known usage; pinned OpenCode completes an edit-and-test loop with its own tools using explicit test configuration |
| 5.1c | Deferred enhancements: incremental envelope content, explicit utility-model opt-in, qualified `response_format`, native token limits for additional profiles and `stop` semantics | P2 | M | 5.1b | 13.2.3, 13.2.5, 13.2.6 | Each added feature is capability-gated and matches streaming/non-streaming behavior; stop matching covers chunk boundaries and never slices tool arguments; explicit model ids stay pinned. Until implemented, these controls remain unsupported; unmodeled request fields are ignored as specified for v1 |
| 5.1d | Deferred investigation only after measured benefit: session-resume identity, concurrent branches/forks, authorization, retained-history expiry/cleanup; `/v1/responses` as separate scope | P3 | L | 5.0, 5.1b | 13.2.10 | Separate reviewed stateful design before implementation; exact prefix and model/tools/profile match required, suffix-only resume, mismatch replay, isolated branches; low cache hits alone do not trigger implementation |
| 5.2 | `relay opencode-config` snippet from the discovered model catalog, CLI-observed client limits/capabilities and explicit token reference; optional plugin injection after hook verification | P1 | M | 5.1b | 13.2.8 | Pinned OpenCode config/provider compatibility verified, including always-sent fields and missing usage; helper needs no plugin or supervisor and writes no token to project files; any plugin injection additionally depends on 3.1 |
| 5.3 | Optional LLM classifier (ties and missing categories) with `llm-classifier` rule id and cost accounting | P2 | M | 4.4, 4.5 | 4.2 | Used only when rules fail; logged in the route log |
| 5.4 | Optional provider-backed advanced planner extension for graphs, beyond the phase-2c CLI coordinator; constrained to graph schema and validation | P2 | L | 5.3, 2.24 | 8.1, 8.7, 17 | Generated graphs always pass validation |
| 5.5 | Pinned OpenCode client acceptance for reasoning variants | P1 | M | 5.1b, 5.2 | 13.2.8 | With the generated config, select low/medium/high for one model in the real OpenCode client; verify each request carries `reasoning_effort`, preserves the model ID and reaches the backend effort argument for JSON and SSE; verify omitted effort keeps the default |
| 5.6 | Verify Agy model metadata and safe effort-based grouping | P2 | M | 5.1a, 5.1 | 13.2.1, 13.2.8 | Inspect authoritative Agy metadata and invoke captured candidates to determine whether `-low`/`-medium`/`-high` slugs can be stripped; test stripped model plus independent `reasoning_effort`; document compatibility evidence and retain/group IDs only when the stripped form is confirmed invocable |
| 5.7 | Close executable-regex gap in bounded tool-schema validation | P1 | M | 5.1b | 13.2.5 | Reject executable regex keywords before backend dispatch or provide genuinely bounded validation; adversarial `pattern` cases cannot bypass validation work/deadline bounds or block shutdown |
| 5.8 | Preserve unknown Agy context/output limits instead of supplying model-family defaults | P2 | S | 5.1a | 13.2.2, 13.2.10 | Responses without metadata remain unknown for both Gemini and Claude slugs; catalog/cache and generated config expose only CLI-observed limits, with absent client `limit` fields |

**Exit criteria (M7 gateway):** 5.0, 5.1a, 5.1, 5.1b and 5.2 acceptance passes, the review defects in 5.7/5.8 are closed, plus an independent security/correctness review of auth, admission/drain and protocol errors. Advertise the configured catalog and tested pinned clients; document subset limitations and unresolved CLI behavior. 5.1c/5.1d, remote serving/TLS, plugin injection and provider-backed planning are optional later work; planner output, whenever shipped, never bypasses validation.

---

## Cross-cutting work

| Area | Tasks | When |
|---|---|---|
| Documentation | Keep `design.md` and this plan in sync (update **[verify]** tags after live checks); changelog; user docs per phase | Every phase exit |
| Testing | Test areas in design section 16 map to phases; add the area's tests inside the task that introduces the behaviour, not later | Continuous |
| Security review | Threat-model review of IPC and apply (phase 2b), environment handling and logs (phase 1), plugin approvals (phase 3), coordinator context broker, path containment and restricted profiles (phase 2c), completion auth and request lifetime (phase 5) | Before M2, M4, M5, M7, M8 |
| Release | Versioning; PyPI (or `uv tool` install from git) for `relay`, npm publish for the plugin; optional PyInstaller binary | M2, M4, M5, M8 |
| Compatibility watch | Track CLI flag changes (Claude, Codex, Antigravity, Gemini, Cursor) and OpenCode plugin API; adapters are snapshot-tested so drift shows up in CI | Ongoing |

## Risks that change the plan

| Risk | Effect on plan | Trigger and response |
|---|---|---|
| Spike 0.11 shows agents do not return `needs_input` reliably | 1.21 shrinks to the manual `relay reply` path; the design's risk row stands | Decide at the end of phase 0 |
| Spike 0.10 finds no qualifying lock or pipe behaviour on a platform | 2b is skipped on that platform; supervisor-dependent plugin/task features need a different story there (foreground only); the standalone phase-5 gateway does not depend on IPC | Decide at the end of phase 0; record the supported matrix |
| Windows filesystem-confining profile unavailable | `workspace` stays unsupported and is not advertised. `readonly` is advertised for every enabled agent without canary promotion | Spike 0.12 and task 2.30 (superseded 2026-10-29) |
| No qualifying restricted coordinator profile (spike 2.31 no-go) | No longer applies: selection only requires an enabled agent with a coordinate capability (2026-10-29) | Coordinator selection; an agent without `coordinate` is listed unavailable |
| CLI flag drift during development | Adapter rework | Snapshot tests and nightly live smoke (1.26) flag it |
| MVP scope creep | Slips M2 | Cut order lists; anything not in the phase 1 tables needs a design change first |

---

## Task status

Only active, deferred, and superseded tasks are listed here. Completed task definitions and their full validation notes are archived in [completed-tasks.md](completed-tasks.md). Status values here: `in progress`, `not started`, `dropped`. Review findings and document changes are recorded in [revision-changes.md](revision-changes.md).

Superseded/removed on 2026-10-29: the CLI-verification and canary mechanisms recorded in 0.8, 1.12, 1.20, 1.21, 2.20, 2.30, 2.32 and 4.11, and in the gateway 5.x rows. Entries below remain as recorded history; they no longer describe current behaviour.

| ID | Status | Date | Note |
|---|---|---|---|
| 0.2 | in progress | 2026-10-26 | Deferred at operator request; minimum/latest Python matrix and cache updates prepared; local matrix checks pass; remote CI remains held |
| 1.10 | in progress | 2026-10-23 | Deferred at operator request; golden checks pass; live Codex 0.164.0 returned a model-capacity error, leaving successful structured/resume validation outstanding |
| 1.21 | in progress | 2026-10-23 | Foreground CLI/TTY protocol implemented and tested; the live Claude clarification check passed at the time (its doctor check was removed 2026-10-29); the Codex live check remains deferred with 1.10 |
| 1.25 | in progress | 2026-10-23 | Integration and five-scenario acceptance suites pass locally on Windows and Linux; remote CI confirmation remains deferred under 0.2 |
| 1.26 | in progress | 2026-10-23 | Validation work deferred at operator request; opt-in tests and procedure exist, with nightly automation and paired live sign-off outstanding |
| 1.28 | in progress | 2026-10-23 | README walkthrough, input examples, reference links and troubleshooting complete; direct and input paths rehearse successfully with fake workers; authenticated new-user walkthrough deferred at operator request |
| 1.30 | in progress | 2026-10-23 | All five stub-CLI scenarios pass on Windows and Linux; M2 sign-off still awaits the deferred CI/Codex checks and remaining P1 release work |
| 2.30 | dropped | 2026-10-26 | Superseded 2026-10-29: `advertised_levels`/`READONLY_VERIFIED_PLATFORMS` were removed; `readonly` is advertised for every enabled agent and `workspace` is not. No promotion or canary work remains |
| 4.1 | in progress | 2026-10-22 | Adapter code complete, 36 tests; the `agy` aliases are enumerated from `agy models` for the gateway. Canary and live-verification gates were removed 2026-10-29 |
| 4.11 | in progress | 2026-10-27 | Historical (canary and doctor checks removed 2026-10-29): doctor checks `adapter.cursor.{version,auth,structured,session,quoting,needs_input}` and `permission.cursor.{readonly,workspace}`, shown only with `agents.cursor.enabled: true`. One live run each on `agent` 2026.10.22-27dcf5f, Windows 11, account login: version, auth, structured, session, quoting and needs_input passed; `readonly` `inconclusive` (no mutation observed, enforcement not confirmed); `workspace` `inconclusive` (the Windows CLI has no filesystem sandbox and refuses `--sandbox enabled`). Recorded in `tests/fixtures/recorded/cursor_doctor_canaries.json`. Remaining: an `enforced` `readonly` result before any promotion; Linux/macOS under 2.33; no new raw envelope capture was made. Follow-up: the permission canary should keep the CLI exit code and a redacted first stderr line so an `inconclusive` result is diagnosable (CU1). Done since: doctor resolves Cursor's command exactly like the adapter (`resolve_cursor_command`: legacy `cursor-agent` only when the built-in `agent` is configured and absent; an explicit `cmd` is never replaced, CU2); the canaries still pass `--trust` because they run in disposable directories. Not confirmed: whether Cursor project hooks or rules depend on workspace trust |

Gateway implementation status (2026-10-29; pinned-client release gates remain separate; completion qualification was removed):

| ID | Status | Date | Note |
|---|---|---|---|
| 5.0 | in progress | 2026-10-29 | Repository/recorded-output findings are retained in `docs/gateway-qualification.md`; the qualification procedure, profile fingerprint and canaries were removed on 2026-10-29 and that document keeps them as history. Operator-authorized `tests/test_gateway_live.py` previously passed both completion invocation modes on Claude 2.1.312/Windows/subscription (2 passed, 12.77 s, then 25.22 s). Growing-history cache/latency measurements, nonempty tool-envelope reliability and pinned OpenCode behavior remain unmeasured; no qualification gate remains |
| 5.1a | in progress | 2026-10-29 | Completion contracts, separate Claude and `agy` backends, usage parsing and bounded process reuse implemented. Offline parser/invocation/cancellation tests pass. Completion results expose the CLI's `modelUsage` model id and context/output limits for discovery. Qualification records and exact-profile rechecks were removed on 2026-10-29. Codex unavailable |
| 5.1 | in progress | 2026-10-29 | Optional FastAPI/uvicorn service and CLI, loopback/bearer boundary, catalog/auto routing, atomic admission, shared deadlines, disconnect/shutdown drainage, buffered SSE and OpenAI error mapping implemented. Offline SDK/local HTTP tests cover auth, limits, slow readers and failure semantics; shared process regressions cover long unterminated lines and unconfirmed drainage. Final profile drills passed before the qualification gate was removed. Catalog ids are configurable CLI aliases (`--model`, default `default`/`sonnet`/`opus`/`fable`); limits are discovered lazily on the first `/v1/models` request by one billed single-flight probe per alias, cached per agent/platform, with probe failures non-fatal and reported on stderr. Fake-backend tests cover parsing, fail-soft, cache and single-flight behavior. Pinned-client/M7 acceptance remains a gate; discovery has not yet been exercised against the live CLI |
| 5.1b | in progress | 2026-10-29 | Deterministic transcript, tool-history/choice checks, strict envelope validation with schema-times-instance work budget, one repair, truthful aggregate usage and buffered tool SSE implemented; offline regressions pass. Pinned OpenCode edit-and-test loop with the actual CLI remains required |
| 5.2 | in progress | 2026-10-29 | `opencode-config` builds the gateway catalog locally from the shared discovery cache (no gateway connection or token needed) and prints explicit provider configuration without persisting credentials. Without `--refresh` it makes no model call and names undiscovered models on stderr; `--refresh` probes (billed) and updates the cache; `--model` selects aliases. Models without CLI-observed context/output limits are listed without limits. CLI/offline tests pass; pinned OpenCode verification remains outstanding; optional plugin injection deferred |
| 5.5 | not started | 2026-10-30 | Reasoning-effort request support and generated OpenCode variants are implemented; real-client verification across low/medium/high and JSON/SSE remains outstanding |
| 5.6 | not started | 2026-10-30 | Agy model metadata and suffix grouping have not been verified; keep CLI-reported aliases intact until invocation evidence confirms safe stripping |
| 5.7 | not started | 2026-10-30 | Review found accepted `pattern` expressions still execute in JSON Schema validation; catastrophic backtracking bypasses the work counter and worker-thread deadline. No runtime fix made in this documentation review |
| 5.8 | not started | 2026-10-30 | Review reproduced Agy assigning Gemini-sized context/output defaults to a Claude slug whose response had no limits; those values are not observations. No runtime fix made in this documentation review |

**Gateway validation evidence (2026-10-29):** final full Windows snapshot 1,695 passed, 16 skipped (1,257.71 s); latest affected-file checks 108 passed, 2 skipped (9.80 s), then 76 affected gateway tests passed after the reviewed plain-text record was added; Ruff lint, full Pyright, formatting of 27 changed Python files and `uv build` passed. Repository-wide format check reports only the pre-existing `tests/test_tui.py` formatting issue. Historical initial and final authorized gateway live drills passed as recorded in `docs/gateway-qualification.md`; final identity was stable across before/after and both modes. This does not complete the separate deferred MVP release checks or pinned OpenCode/M7 acceptance.

**Final graph validation evidence (2026-10-26):** full post-graph-fix suites passed on Windows (1,255 passed, 12 skipped; 647.58 seconds) and Linux (1,254 passed, 11 skipped; 231.61 seconds). Subsequent focused standalone/reuse/retention/graph-integration regressions passed on Windows (52 passed, 1 skipped across retry, input, recovery and run; plus 2 logical-model provenance cases) and Linux (187 passed, 1 skipped across retry, input, recovery, run, run CLI, graph, reuse, Claude and events; plus 2 logical-model provenance cases). After the last source patch, Ruff check, Ruff format check across all 127 Python files, full Pyright and `uv build` passed. Independent review found no remaining issues in graph recovery/cancellation, standalone retry history/model overrides, permission caps, inline event sequencing or reuse/cache retention. No live CLI/model calls or remote CI ran; those MVP validation gates remain held.


### Remaining work in order

The status table above tracks active and deferred work. Completed task definitions and validation evidence are preserved in [completed-tasks.md](completed-tasks.md). Phase 2a
implementation is complete and phase 2c implementation (2.20 to 2.28, plus spike 2.31 and task
2.32) is implemented and tested on Windows, where 2.29 completed acceptance and hardening. M3
and M8 sign-off, along with release exit gates, remain as defined above. Deferred MVP
validation remains separate lower-priority release closure.

**Gateway v1 implementation and client acceptance**

- Close the bounded-validation defect in **5.7** before claiming the schema work/deadline guarantee. Correct the observed-only Agy limit gap in **5.8** before treating its catalog/client budgets as trustworthy; both findings are recorded in [revision-changes.md](revision-changes.md).
- Gateway tasks 5.0/5.1a/5.1/5.1b/5.2 have historical full and focused offline coverage and authorized live drills; completion qualification was removed. This review adds the open defects above; pinned-client acceptance and measured CLI metadata remain outstanding as recorded in the status table. See `gateway-qualification.md`; the catalog serves enabled agents with completion adapters.
- Keep 5.1c/5.1d deferred. Complete measured context/output metadata, growing-history/cache observations and pinned OpenCode acceptance before M7 claims. Other future work remains in the phase-2b/3/4 tables and is independent of this foreground gateway where its dependencies permit.

**Before M3 sign-off**

- **2.30 (superseded 2026-10-29):** qualification-based `readonly` promotion and
  `READONLY_VERIFIED_PLATFORMS` were removed. `readonly` is advertised for every enabled agent and
  `workspace` is not; no read-node canary or promotion work remains under this task, and it no
  longer gates M3.

**Release gate for M8**

- **Coordinator selection (revised 2026-10-29):** qualification records and digests were removed.
  An enabled agent that offers a coordinator is selected as requested → `claude` → first sorted,
  and the recorded proposer is the agent name. M8 sign-off relies on the remaining acceptance work
  below rather than a reviewed record.

**Phase 2c implementation (M8), remaining**

1. **2.33 (P2, lower priority):** Linux/macOS acceptance (full offline and Pilot suites, including
   the 2.29 observer, cancel-safety and purge regressions) and a real-terminal smoke test of
   `relay tui` (Windows included: the 2.29 evidence is Pilot-driven, not an interactive terminal
   session). Qualification and `readonly`-promotion items were removed on 2026-10-29. A first Linux
   run (RHEL 8, Python 3.11) was started during 2.29 and is not yet reviewed.

Implemented and not repeated here: 2.20 contracts (qualification matrix superseded), 2.21 sessions
and ownership, 2.22 controller, 2.23 context broker, 2.24 planning loop, 2.25 checkpoints and
retained bases, 2.26 progress streaming, 2.27 cumulative delivery, 2.28 Textual TUI, 2.29 Windows
acceptance and hardening, 2.31 spike, 2.32 coordinate capability.

**Lower-priority MVP release closure (P1)**

1. **0.2:** obtain a green remote Windows/Linux CI run. This can proceed independently of 1.10.
2. **1.10:** complete successful live Codex structured-output and session-resume checks.
3. **1.21:** complete the remaining Codex clarification live check after 1.10.
4. **1.25:** confirm integration coverage in CI after 0.2 and 1.21 close.
5. **1.26:** finish paired live smoke sign-off and nightly automation after 1.10.
6. **1.28:** complete the authenticated new-user walkthrough; 1.24 is already done, so it can proceed independently of the CI/Codex checks.
7. **1.30:** finalize acceptance and M2 sign-off after the required phase-1 tasks close.

These checks remain required for M2 and M3 sign-off. Their current validation deferral remains
in effect; this ordering does not start new CI or live-agent runs.

**Optional work (P2)**

- **1.6:** implement `relay run --dry-run` and preview route entries; it does not block M2 or graph implementation.
