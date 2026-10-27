# Agent Relay: Multi-Agent CLI Router

Status: Draft (phase 2c foundations and cumulative session delivery implemented; TUI and release qualification pending) · Scope: programming tasks

This document is the specification: it states each rule once, in the section named in the map below, and other sections refer to it. Change history is in [`revision-changes.md`](revision-changes.md), the task breakdown in [`development-plan.md`](development-plan.md), and the rule for which file holds what in the documentation rules of [`AGENTS.md`](../AGENTS.md).

A router that takes software-engineering tasks, decides which agent CLI (Claude Code, Codex, Antigravity CLI, Gemini CLI, Cursor, ...) and which model should do each one, runs each task in an isolated checkout, and passes context and results between tasks as specs (files). Phase 1 executes one node in the foreground; concurrent graphs arrive in phase 2a (section 16). Its front-ends share one core: the standalone `relay` CLI, a foreground conversational TUI in phase 2c, and an OpenCode plugin in phase 3. The core, CLI and Textual TUI are Python; only the OpenCode plugin requires TypeScript (section 15).

### Document map

| Topic | Canonical section |
|---|---|
| Goals, assumptions, phase scope | 1, 16 |
| Components and process model | 2, 8.6, 8.7 |
| Registry, merge and rule semantics, pricing, risk, paths | 3, 4.1 |
| Agent and model routing, route log | 4 |
| Spec formats (task, result, input answers), conversation/plan/approval/context contracts (5.6) | 5 |
| Run/session store layout, ownership/locks, recovery, retention, runtime-state exclusion, reuse keys | 6 |
| Adapters, per-CLI flags, session ids, Windows spawning, coordinator/progress capabilities (7.4) | 7 |
| Task graph (8.1), scheduling and the `needs_input` protocol (8.2), isolation/merge and delivery/`apply` (8.3), spawn requests (8.4), cost accounting (8.5), supervisor/IPC (8.6), interactive coordination/checkpoint rounds (8.7) | 8 |
| Permission levels, enforcement, environment allowlist, injection hygiene | 9 |
| Verification | 10 |
| Events and observability | 11 |
| OpenCode plugin, `relay` CLI, TUI entry points and interaction (14.1), layout | 13, 14, 15 |
| Roadmap, tests, risks | 16, 17 |

### Phase availability

Sections describe the full design. Not everything exists from the first release: section 16 lists what each phase delivers, and the MVP (phase 1) is a deliberately thin slice: a foreground, single-node `relay run` with routing, the route log, worktree capture and verification, and git-mode `relay apply`. Features that arrive later are tagged in place (for example "phase 2" or "phase 4") and are rejected as unsupported, rather than silently ignored, before they exist.

---

## 1. Goals, non-goals, assumptions

### Goals
- Route programming tasks (implement, bugfix, refactor, test, review, investigate, migrate, docs-for-code, research, design) to the best-fit agent CLI.
- Per-agent **model routing**: each CLI has its own model catalog and its own rules for choosing a model.
- Run **multiple sub-agents concurrently**, each invoking a different CLI, with dependency ordering (task DAG), fan-out and fan-in.
- Exchange context and output between agents only through **versioned, validated specs** (files), not transcripts.
- **Highest permission by default** for worker CLIs, with an explicit parameter to restrict; coordinator calls require the restricted capability in section 7.4.
- Conversational planning, visible task dispatch and structured handoffs through the foreground TUI, with explicit plan and delivery approval (sections 5.6, 8.7, 14.1).
- CLI, TUI and OpenCode front-ends over one shared execution core.

### Non-goals
- Not a new coding agent; the CLIs do the coding.
- Not a general-purpose (non-programming) assistant router.
- No hosted service or multi-machine scheduling in v1 (local processes only).
- No re-implementation of the CLIs' own tool loops, MCP handling or authentication. Users log in to each CLI themselves.

### Assumptions
- Each CLI is installed and authenticated on the machine; the router only discovers and invokes it.
- Python 3.11 or newer is available on the host (Windows, Linux; macOS best effort). Node is needed only to run OpenCode and its plugin, never by `relay` itself.
- Work happens in a git repository (needed for worktree isolation, delta capture, and diff verification). Git is a strict requirement in phases 1 to 3: running in a non-git directory fails with instructions to run `git init`. From phase 4, `relay` can instead prompt to initialize Git or automatically initialize an isolated shadow Git repository under `.agents/shadow-git/` so that worktree and commit verification mechanics remain fully operational. The shadow repository's baseline commit (a snapshot of the current non-ignored files) is created before the run starts, so a freshly created shadow repository counts as a clean tree for the check in section 9.4. In shadow-git mode (phase 4), verified deliverables from the integration commit are synced back into the user's workspace directory via `relay apply <run>` (sections 8.3, 14).
- CLI flag and output-shape evidence applies to the specific tool versions named in sections 5.4 and 7.2. Capture provenance is recorded in `tests/fixtures/recorded/README.md` and the revision log. Flag existence does not establish runtime behaviour or enforcement; unconfirmed claims are marked **[verify]** and require the corresponding `relay doctor` canary. Offline fixture replay is not a fresh live validation.

---

## 2. Architecture

```
                      ┌────────────────────────── front-ends ──────────────────────────┐
                      │  relay CLI / Textual TUI        OpenCode plugin (plugin/, TypeScript)
                      │  commands / SessionController  tools: route_task, run_agent_task, run_task_graph...
                      │                               optional: OpenAI-compatible provider gateway
                      └───────────────┬─────────────────────────────┬───────────────────┘
                                      ▼                             ▼
┌──────────────────────────────── relay.core ──────────────────────────────────────────────┐
│ Intake ─▶ Classifier ─▶ Planner (DAG) ─▶ Router ─▶ Scheduler ─▶ Worker ─▶ Adapter ──▶ CLI process
│                             │               │         │            │      (claude|codex|agy|gemini|cursor)
│                             │         ModelRouter     │        Worktree mgr                     │
│                             ▼               │         ▼            │                            ▼
│                       Spec store  ◀─────────┴──── Verifier ◀───────┴──────────────  result.spec.yaml
│                  (.agents/runs/<run>/…, events.jsonl)
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

Responsibilities:

| Component | Responsibility |
|---|---|
| Intake | Accepts a free-text request or a prebuilt task/graph spec; gathers repo facts (root, base branch, languages, build/test commands from `PROJECT.spec.md`). |
| Classifier | Assigns a category and complexity to a task using deterministic rules first, optional cheap-LLM fallback. |
| Planner | Decomposes a request into a task DAG (`depends_on`, `owns`). Can be a template pipeline, a user-supplied graph, or LLM-proposed (in OpenCode, the host LLM plans and submits the graph). |
| SessionController (phase 2c) | Application service owning a conversation, bounded coordinator calls, approvals, owner commands and linked foreground runs; delegates execution to the existing core (section 8.7). |
| Router | Picks the agent for each node (section 4). |
| ModelRouter | Picks model and reasoning effort inside the chosen agent using that agent's own rules (section 4.3). |
| Scheduler | Runs ready nodes on a bounded worker pool with limits, timeouts, retries, fallback, cancellation. |
| Worker | One supervised OS process wrapping one adapter invocation in its own worktree. |
| Adapter | Translates a task spec into a CLI invocation and the CLI output back into a result spec. |
| Verifier | Runs the task's `acceptance` commands itself and checks diff scope. |
| Store | Run directory with specs, logs, artifacts, state, event log. |
| Supervisor | Phase-2b detached owner of Scheduler, Workers, Verifier and Store writes, serving authenticated IPC (sections 6, 8.6). Foreground execution does not require it. |
| Applier | Shared delivery service for CLI, TUI and `task_apply`: prepares, verifies and confirms delivery into the user's branch or workspace (sections 8.3, 10). |

Dependency rule: `core` has no OpenCode or Textual dependency. The TUI calls the application service in process; widgets neither invoke the CLI recursively nor implement routing, scheduling, verification or persistence. The plugin contains no routing or run logic; it reaches the core through the `relay` executable (`--json` output) and supervisor IPC (section 8.6). Front-ends share `agents.yaml`, spec schemas and run records; availability and ownership rules still apply when another front-end inspects or resumes a run.

---

## 3. Agent registry (`agents.yaml`)

Routing is data, not code. Merge order: built-in defaults → `~/.config/relay/agents.yaml` (user) → `./.agents/agents.yaml` (project). Later files override earlier ones. Maps merge recursively by key; primitive arrays replace the earlier array in full (including `categories`, `strengths`, and `fallback`). Rule lists (`routes.rules` and each agent's `model_routing.rules`) merge by matching rule `id`: rules with matching `id` override properties or can be disabled via `enabled: false`, while new rule IDs are appended (all rules require an `id`). Explicit `false`, `0`, and empty arrays are retained rather than treated as missing. `null` is rejected unless the field's schema explicitly permits it; it does not delete inherited keys. The merged config is validated and snapshotted in the run store. Permission restrictions additionally follow section 9.2.

Rule IDs are required and unique within each source list. A matching ID keeps its inherited position; nested `when` and action blocks (`prefer` or `use`) replace those blocks in full when supplied. `enabled: false` disables a rule before evaluation. Optional integer `priority` (default 0) orders enabled rules descending, with merged list position as the stable tie-breaker. New project exceptions can precede general inherited rules by setting a higher priority. An empty keyed list adds no overrides; disabling inherited rules requires their IDs. Anonymous rules and duplicate IDs are invalid. Rule predicates in `when` blocks are evaluated conjunctively (all specified attributes must match); array values within an attribute match disjunctively (any value matches); numeric attributes (`est_tokens`, `attempt`, `files`) and ordered complexity levels (`low` < `medium` < `high`) support comparison operators (`<`, `<=`, `>`, `>=`, `=`). Pricing rates in `agents.<agent>.pricing` specify USD per 1M tokens; if `cache_read_per_m` or `cache_write_per_m` are omitted, the settlement computation uses `input_per_m` for that bucket as a placeholder; labeling and hard-mode consequences are defined in section 8.5, and `relay doctor` warns when an agent that reports cache tokens lacks explicit cache rates.

```yaml
version: 1
defaults:
  permissions: full            # full | workspace | readonly  (section 9)
  concurrency: 4               # global worker cap
  timeout_s: 1800              # per attempt (budget.timeout_s); cumulative node limit is budget.max_duration_s
  fallback: [codex, claude, gemini]

route_log:
  enabled: true                # section 4.4
  include_goal: true           # false: omit goal-derived text, retain operational metadata
  title_max_chars: 120

risk:                          # section 4.1; replaces the default list when supplied
  paths: ["**/auth/**", "**/security/**", "**/migrations/**", ".github/workflows/**", "**/Dockerfile*", "**/infra/**", "**/*.tf"]

paths:                         # section 6 / 8.3
  worktree_root: auto          # auto: .agents/worktrees; on Windows a short root (see section 8.3). Or an explicit absolute path.

routes:                        # agent selection rules (section 4.2); highest effective priority first, first match wins
  rules:
    - id: route-bugfix-codex
      when: { category: [bugfix], language: [typescript, python] }
      prefer: [codex, claude]  # candidates, ordered; availability and load still apply
    - id: route-impl-codex
      when: { category: [implement, test, migrate] }
      prefer: [codex, claude]
    - id: route-review-claude
      when: { category: [review, architecture] }
      prefer: [claude]
    - id: route-research-gemini
      when: { category: [codebase-research] }
      prefer: [gemini, claude]
  default: score               # no rule matched: category/strength scoring

agents:
  claude:
    enabled: true
    cmd: claude
    categories: [refactor, review, architecture, debug, implement]
    strengths: [large-refactor, code-review, multi-file-reasoning]
    cost: high
    concurrency: 2
    pricing:                     # ILLUSTRATIVE numbers only: set from the provider's current price list (USD per 1M tokens)
      claude-haiku-5-5:  { input_per_m: 1.00,  output_per_m: 5.00,  cache_read_per_m: 0.10, cache_write_per_m: 1.25 }
      claude-sonnet-5-5: { input_per_m: 3.00,  output_per_m: 15.00, cache_read_per_m: 0.30, cache_write_per_m: 3.75 }
      claude-opus-5-5:   { input_per_m: 15.00, output_per_m: 75.00, cache_read_per_m: 1.50, cache_write_per_m: 18.75 }
    models:                      # logical tier -> real id; ids only live here
      fast:     claude-haiku-5-5
      balanced: claude-sonnet-5-5
      deep:     claude-opus-5-5
    model_routing:               # section 4.3
      rules:
        - id: claude-deep-review
          when: { category: [review, architecture], complexity: ">=high" }
          use: { model: deep }
        - id: claude-impl
          when: { category: [implement, refactor, bugfix] }
          use: { model: balanced }
        - id: claude-cheap
          when: { category: [docs-for-code], est_tokens: "<5000" }
          use: { model: fast }
      default: { model: balanced }
      escalate_on_failure: [fast, balanced, deep]

  codex:
    enabled: true
    cmd: codex
    categories: [implement, bugfix, test, migrate]
    cost: medium
    concurrency: 3
    pricing:
      <id>: { input_per_m: 2.50, output_per_m: 10.00 }
    models: { fast: <id>, balanced: <id>, deep: <id> }
    model_routing:
      rules:
        - id: codex-fast-tests
          when: { category: [test], complexity: "<=medium" }
          use: { model: fast, effort: low }
        - id: codex-deep-hard
          when: { category: [bugfix, implement], complexity: ">=high" }
          use: { model: deep, effort: high }
      default: { model: balanced, effort: medium }

  gemini:
    enabled: true
    cmd: gemini
    categories: [codebase-research, docs-for-code]
    cost: low
    pricing:
      <id>: { input_per_m: 0.50, output_per_m: 1.50 }
    models: { fast: <id>, deep: <id> }
    model_routing: { default: { model: fast } }

  cursor:
    enabled: false               # opt-in
    cmd: agent                   # legacy alias: cursor-agent
    categories: [implement, refactor]
    models: { default: auto }
```

**Built-in defaults** are the example above with three differences. `pricing` is omitted, because rates are user data (section 8.5). Agents without a verified model id list (Codex, Gemini) map every tier to the literal `default`, which the adapter treats as "pass no model flag, use the CLI's own configured model"; a user config replaces those tiers with real ids. Unknown keys are rejected everywhere in `agents.yaml`, so a misspelled key cannot silently disable a rule. The permitted per-agent keys are `enabled`, `cmd`, `categories`, `strengths`, `cost`, `concurrency`, `permissions`, `env`, `pricing`, `models` and `model_routing`. Rule `when` attributes are `category`, `complexity`, `language`, `risk`, `est_tokens`, `files`, `attempt` and `cost_ceiling`; category values must be those of section 4.1.

Capability probing: at startup (and via `relay doctor`) each enabled agent is probed with `<cmd> --version` and `--help`. Missing or unauthenticated agents are marked unavailable and skipped by routing; they never fail a run unless a task pins them explicitly.

---

## 4. Task taxonomy and routing

### 4.1 Categories (programming only)

| Category | Typical signals |
|---|---|
| `implement` | "add", "build", feature description, acceptance criteria |
| `bugfix` | stack trace, failing test, "crash", "regression" |
| `refactor` | "rename", "extract", "restructure", no behaviour change |
| `test` | "write tests", coverage, flaky test |
| `review` | diff/PR/branch reference, "review" |
| `debug` | "why does", logs, repro steps, no fix requested |
| `migrate` | dependency/framework/version upgrade |
| `docs-for-code` | docstrings, README, API docs |
| `codebase-research` | "where is", "how does", architecture questions |
| `architecture` | design/plan/spec requests |

Complexity (`low|medium|high`) is estimated from: number of files/symbols touched, size of the referenced diff, estimated context tokens, cross-module reach, `risk` (below), and prior failures of the same task. An `elevated` risk raises complexity by one level (capped at `high`).

`risk` (`normal|elevated`) is a rule input derived deterministically: it is `elevated` when any path in `context.files`, `owns` or `diff_scope` matches a risk glob, or the task goal names one of the configured risk keywords. Risk globs come from `risk.paths` in `agents.yaml` (defaults: `**/auth/**`, `**/security/**`, `**/migrations/**`, `.github/workflows/**`, `**/Dockerfile*`, `**/infra/**`, `**/*.tf`) and can be extended in `PROJECT.spec.md`. Matching is recorded in the route log `reason`.

### 4.2 Agent selection

Every decision records the rule that produced it (`agent_rule` in the route log, section 4.4). Rule ids come from `routes.rules` in `agents.yaml`, or are one of these fixed implicit ids: `pinned`, `category-match`, `score`, `llm-classifier`, `fallback`, `escalation`.

1. **Pinned** (`agent_rule: pinned`): the task or CLI flag names an agent (`--agent codex`) → use it (if available).
2. **Routing rules** (cheap, deterministic): the classifier maps request signals to a category and complexity; the first matching enabled rule in effective priority order yields an ordered `prefer` list (`agent_rule: <rule id>`). Filter candidates for availability, permissions, and model capability. Choose the first eligible candidate with capacity; if eligible candidates are only busy, queue for capacity rather than declaring them unavailable. If none is eligible, use the score/default fallback policy and record why the preferred list was exhausted. A pinned agent queues when busy and fails when unavailable or incapable; it is never silently replaced.
3. **Score** (`agent_rule: score` or `category-match`): when no rule matches (`routes.default: score`), availability is a hard filter applied before scoring, not an additive term. Each remaining candidate scores `+1.0` when the task category is in the agent's `categories`, else `0`; `strengths` contributes `0.0` (the field is still recorded but has no deterministic task→strength signal yet, so it never changes the order); minus a cost penalty (`low` 0.0, `medium` 0.1, `high` 0.2, unset 0.0); minus `0.5` per recent failure; minus `0.1` per running attempt. Highest wins; ties break by `defaults.fallback` order. A single eligible candidate whose `categories` contain the task category is recorded as `category-match`.
4. **LLM classifier (optional)** (`agent_rule: llm-classifier`): only when rules yield no category or a tie; a single cheap call returning `{category, complexity}`. In OpenCode the host model can provide this directly through `route_task` input.
5. **Fallback** (`agent_rule: fallback`): on adapter failure, timeout, or unavailable agent, retry with the next candidate; record why. A model-tier retry on the same agent is recorded as `escalation`.

### 4.3 Model selection inside an agent

Every agent owns an independent `model_routing` block. The router never shares model rules across agents. The model router receives `(agent, task spec)` and returns `{model, effort, model_rule}`:

- Enabled rules are evaluated in effective priority order (section 3); first match wins; otherwise `default`.
- Inputs: `category`, `complexity`, `est_tokens`, `files` count, `language` (resolved hierarchically: explicit `task.language` -> inferred from dominant extension in `context.files`/`owns` -> `repo.language` default), `risk` (section 4.1), `cost_ceiling` (remaining run budget in USD; available only from phase 4, and a rule that uses it is rejected at config validation before then), `attempt` (retry number).
- Outputs: a logical tier or literal model id, plus optional reasoning `effort`, the matching `model_rule` id (or `default`), and the resolved model pricing basis from `agents.yaml`.
- `escalate_on_failure` (an ordered list of tier names, lowest to highest; when an agent omits it, the default is that agent's `models` tier names in declaration order, and an agent with a single tier never escalates): if a node fails its acceptance checks, the retry moves one tier up the list, subject to budget admission (section 8.5). If the node is already at the highest tier in `escalate_on_failure`, or if the resolved model is a literal ID not present in the tier list, subsequent retries remain at that model until `max_attempts` is exhausted or cross-agent fallback triggers. A tier present in `models` but omitted from an explicit `escalate_on_failure` list is outside the escalation path: a node resolved to that tier stays there and never escalates, like a literal model id. A cheaper model may be selected when the next attempt would exceed the remaining budget; downgrading alone does not enforce a cost ceiling.
- If the resolved model is unavailable (probe or runtime error) the agent's default model is used and a warning is logged.
- The decision is stored in the task spec (`routing: {agent, model, effort, agent_rule, model_rule}`) and in the route log (section 4.4) with the same field names.

Per-adapter model flags: Claude `--model`; Codex `-m` plus `-c model_reasoning_effort=<level>` **[verify key name]**; Antigravity `--model` plus `--effort`; Gemini `-m`; Cursor `--model`.

### 4.4 Route log

The router keeps a log of every routing decision: which task went to which agent and model, a short description of the task, and the rule that matched. It answers "why did this task go to that agent" without reading prompts or transcripts, and `relay explain` reads the same entries.

**Files** (per run; the orchestrator is the single writer, as in section 6):
- `.agents/runs/<run-id>/routes.jsonl`: derived, append-only projection, one JSON object per decision; rebuildable from durable routing events.
- `.agents/runs/<run-id>/route.log`: human-readable, one line per decision, written by the same writer from the same entry, e.g.

```
2026-10-22T09:41:03Z #7 n2-impl-a  implement/high  -> codex/deep(high)  agent_rule=route-impl-codex model_rule=codex-deep-hard  "Debounce autosave handler"
2026-10-22T09:47:55Z #9 n2-impl-a  implement/high  -> claude/deep       agent_rule=fallback       model_rule=claude-impl        "Debounce autosave handler"  (from codex: timeout)
```

There is deliberately no shared project-wide file: runs can execute concurrently, and per-run files keep a single writer. `relay routes` (section 14) reads across run directories.

**Entry schema** (`schemas/route-entry.json`, generated from the pydantic models like the specs):

```json
{ "ts": "2026-10-22T09:41:03Z", "run_id": "2026-10-22-a1b2", "seq": 7,
  "node_id": "n2-impl-a", "attempt": 1,
  "event": "routed",
  "title": "Debounce autosave handler",
  "category": "implement", "complexity": "high",
  "agent": "codex", "model": "<resolved id>", "effort": "high",
  "agent_rule": "route-impl-codex",
  "model_rule": "codex-deep-hard",
  "reason": "category=implement matched route-impl-codex; codex preferred, available, load 1/3",
  "alternatives": [{ "agent": "claude", "score": 7.9 }],
  "from": null,
  "permissions": "full",
  "executed": true,
  "resumed": false }
```

| Field | Meaning |
|---|---|
| `event` | `routed` (first routing of an attempt), `rerouted` (the routing of a node that already had a decision is recomputed because its inputs changed: `relay resume` with a different `--agent`/`--permissions`, a changed `agents.yaml` snapshot accepted on resume, or new operator answers that change the category/complexity), `fallback` (different agent after a failure), `escalated` (higher model tier, same agent), `pinned` (agent chosen by `--agent`/task), `previewed` (`route_task` dry run, `executed: false`), `rejected` (no eligible agent; `agent` is null). |
| `title` | Short description of the routed task, at most `route_log.title_max_chars` (default 120). |
| `agent_rule` | Matching agent-selection rule: a `routes.rules` id or an implicit id (section 4.2). |
| `model_rule` | A `model_routing.rules` id, `default`, or `escalation` when a tier move (not a rule) selected the model. |
| `reason` | One-line human explanation of the decision. |
| `alternatives` | Other candidates with scores, for `relay explain`. |
| `from` | For `fallback`, `escalated` and `rerouted`: `{agent, model, reason}` of the previous attempt. |
| `seq` | Monotonic per run, so gaps are detectable. |

**Rules**
- **When written:** for every Router decision: initial routing, each retry with model escalation, each fallback, pinned routes, rejections, and `route_task` previews. The entry is written before the worker starts, so a crash still leaves the record. Reusing a `done` node on resume is not a route (it is a `node.reused` event); re-running a node on resume writes a new entry with `resumed: true`.
- **Short description:** the node's optional `title` (supplied by the planner or the user); otherwise derived from `goal` (first sentence, whitespace collapsed, truncated). It never includes the rendered prompt, file contents, or environment values; strings matching known secret patterns are redacted — at minimum URL userinfo, well-known token prefixes (`sk-`, `ghp_`/`gho_`, `AKIA`/`ASIA`, `xox*`, `AIza`, `ya29.`, JWT `eyJ…`), and `key=value` credential assignments. With `route_log.include_goal: false`, omit titles and free-text reasons (including `from.reason`) from canonical routing events and both projections and use a structured reason code instead: `reason` becomes `agent_rule=<id>;model_rule=<id>` followed by optional `;queued`, `;rejected`, `;exhausted=<agent,...>` and `;model=unavailable`, and `title` and `from.reason` are null; operational IDs, agent/model, rules, scores, timestamps, and permissions remain. This setting applies to routing records; prompts, stdout, and result artifacts have separate retention and access policies.
- **Busy and exhausted candidates (phase 1):** the entry persists `alternatives` but not the `busy` (queued-for-capacity) or `exhausted` candidate lists as fields. With `route_log.include_goal: false` they surface through the structured reason code (`;queued`, `;exhausted=<agent,...>`); with `include_goal: true` only the free-text `reason` describes them. Phase 2 may add structured fields for them; phase 1 does not.
- **Failure handling:** a write failure in either route-log projection raises a `route.log.write_failed` warning and never blocks execution. `events.jsonl` is the canonical source: the complete routing entry is durably appended there before starting an attempt. Failure to persist that event prevents dispatch and reports a store error. On resume, replay missing projection entries idempotently by `seq`; discard only a torn trailing record, never a valid earlier entry. With `route_log.enabled: false`, rebuilding is a no-op: no projection is written or repaired, existing projection files are left untouched and must not be treated as current, and readers use `events.jsonl` as the source of truth. `relay explain` and `relay routes` (section 14) read canonical `node.routed` events directly from `events.jsonl`, never the projection, so a missing or stale projection never hides a decision.
- **Relation to `events.jsonl`:** `node.routed` carries `{node_id, route_seq, decision}` with the complete entry. Routing fields are authored once in this event and rendered into both projections. `route_log.enabled: false` disables projections, not durable routing events. A pre-start entry's `executed: true` means execution was admitted, not that a child actually started; `node.started` is the evidence of process startup.
- **Retention:** the files live in the run directory and are removed with it (`relay clean --purge-runs`); ordinary worktree cleanup preserves logs and output refs. Active runs are never pruned; no log rotation is needed.

---

## 5. Spec contract

The artifacts below (except `PROJECT.spec.md`) are YAML (JSON accepted), validated by JSON Schemas generated from the pydantic models in `relay.core.specs` (published under `schemas/`). Every spec has `spec_version`.

### 5.1 `PROJECT.spec.md` (shared, human-owned)

Repository conventions every agent receives: layout, language/toolchain, how to build/test/lint, coding standards, forbidden areas. The adapter also mirrors it into each CLI's native instruction channel (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, Cursor rules) via a generated include or an appended system prompt (e.g. Claude `--append-system-prompt`), without overwriting user-owned files.

### 5.2 `task.spec.yaml` (router → worker)

```yaml
spec_version: 1
run_id: 2026-10-22-a1b2
id: n3-implement-autosave
title: "Debounce autosave handler"       # optional short description for the route log (section 4.4)
category: implement
language: typescript                     # optional task language override; defaults to repo.language
goal: "Add 300ms debounce to the autosave handler"
repo:
  root: .
  base_branch: main
  input_commit: <resolved dependency snapshot sha>  # orchestrator-owned
  language: typescript
context:
  files: [src/editor/autosave.ts]
  symbols: [AutosaveHandler.flush]
  from:                                  # upstream results to read
    - { node: n2-research, field: summary }
    - { node: n2-research, artifact: notes.md }
constraints: [no new dependencies, keep public API stable]
commands:
  build: "npm run build"
  test:  "npm test"
  lint:  "npm run lint"
verify: default                          # default when omitted; section 10
acceptance:                               # run by the Verifier, not the agent
  - { run: "npm test -- autosave", expect_exit: 0 }   # executable check
  - { diff_scope: ["src/editor/**", "tests/**"] }     # scope check only, not an executable check (section 10, item 3)
depends_on: [n2-research]
owns: ["src/editor/autosave.ts", "tests/autosave.test.ts"] # planning-time overlap detection
permissions: full                          # optional restriction (section 9)
routing: { agent: codex, model: balanced, effort: medium, agent_rule: route-impl-codex, model_rule: default }  # orchestrator-owned, same ids as the route log (section 4.4)
budget: { timeout_s: 600, max_duration_s: 1800, max_cost_usd: 2.0, max_attempts: 2 } # timeout_s per attempt, max_duration_s cumulative across attempts; max_cost_usd is phase 4, rejected as unsupported before then (section 17)
output: { result: result.spec.yaml, artifacts_dir: artifacts/ }
```

Adapters render the goal together with supplied constraints, file/symbol references, ownership hints, executable acceptance and default commands into the worker prompt. File references identify content in the isolated checkout; they do not embed repository text as trusted instructions. Verification policy remains orchestrator-owned under section 10.

### 5.3 `result.spec.yaml` (worker → router and downstream nodes)

```yaml
spec_version: 1
id: n3-implement-autosave
status: done            # done | failed | needs_input | blocked | cancelled
agent: codex
model: <resolved id>
summary: "Added debounce(300ms) in AutosaveHandler; tests updated."
changes:
  branch: relay/2026-10-22-a1b2/n3
  input_commit: <resolved dependency snapshot sha>
  output_commit: <orchestrator-created verified sha>
  commits: [<sha>]
  files: [src/editor/autosave.ts, tests/autosave.test.ts]
artifacts: [{ path: artifacts/diff.patch, type: diff }]
decisions: ["Used a local debounce helper to avoid a new dependency"]
verification:           # filled by the Verifier
  mode: default
  acceptance: passed
  details: [{ run: "npm test -- autosave", exit: 0 }]
open_questions: []       # for needs_input: [{id, text, kind: free|choice, options?, default?, required}] with question_revision (section 8.2)
# phase 4
spawn_requests:          # optional follow-up work proposed by this node
  - { category: review, goal: "Review debounce edge cases", depends_on: [n3-implement-autosave] }
usage: { duration_s: 212, tokens_in: 0, tokens_out: 0, cost_usd: 0.0 }
session: { cli_session_id: "<id>" }
```

### 5.4 Rules
- **Validation**: every result is validated. On failure the worker's output is returned once with the validation error appended ("fix your result spec"); a second failure marks the node `failed`.
- **Structured output** is requested natively where supported, with the schema generated from the pydantic models. Claude: `--output-format json` with `--json-schema '<inline JSON schema>'`; the adapter removes the schema's `$schema` key, which the CLI rejects, and nonfunctional schema titles to keep Windows shim launches within the command-line limit, and reads the validated object from `structured_output` (the same JSON also appears as text in `result`). Codex: `--output-schema <file>` with `-o <file>` for the last message; the API accepts only strict schemas, so the adapter first converts the schema (every property listed in `required`, `additionalProperties: false` on every object, each optional property made nullable) and drops the `null` values from the reply before validating it, otherwise the request fails with `invalid_json_schema` before any generation. If a canary shows a CLI does not honour its schema flag, fall back to the schema injected in the prompt instructions. Antigravity supports a native schema flag (`--json-schema`, section 7.2) and returns the validated object in `structured_output`; legacy Gemini and Cursor have no schema flag: the adapter instructs the agent to return the result JSON (Cursor returned it as text in `result`; it may also write `result.spec.yaml` in the node directory) and the parser normalises it before validation: `null` values are dropped, and an empty `options` list on a `free` question is removed (Cursor emits both).
- **Agents write only their node directory** for specs; code changes go to their worktree. The orchestrator fills `changes` from its own change capture (section 8.3), plus `verification`, `usage`, and `status` finalization. These are ownership rules, not enforcement (section 9).
- **Specs carry summaries and file references, never full transcripts.** Downstream nodes get only the upstream fields named in `context.from`.
- **Untrusted content**: text coming from another agent's result is wrapped and labeled as data in downstream prompts (section 9).
- **Human-in-the-loop**: an agent that needs operator clarification returns `status: needs_input` with structured queries in `open_questions`; the protocol (blocking, answers, limits, trust labeling) is defined in section 8.2. If `status` is `done`, populated `open_questions` are only advisory notes for downstream reviewers.
- **Versioning**: `spec_version` is checked; unknown major versions are rejected, unknown optional fields are preserved.

### 5.5 `input_answers.yaml` (operator answers for a `needs_input` node)

Append-only file in the node directory; protocol and limits are in section 8.2.

```yaml
spec_version: 1
node: n3-implement-autosave
submissions:
  - submission_id: 5b0c1e9a        # idempotency key; duplicates have no effect
    question_revision: 1           # must equal the current question set revision
    provenance: operator-tty       # operator-tty | operator-cli | host-submitted | host-attested
    submitted_at: "2026-10-22T09:50:11Z"
    answers: { q1: "300ms", q2: ["a", "b"] }   # question id to string or list of strings
```

`host-submitted` is input relayed through a model tool and is never elevated to trusted operator policy; `host-attested` requires the host to attest operator authorship. The content hash of the answers (not the content) is what appears in events and in the input key.

### 5.6 Conversation, coordination and handoff contracts (phase 2c)

These are additive, versioned contracts; they do not reinterpret legacy task, graph or run records. A conversation is called a **session** in the CLI and store. Its ID is distinct from a run ID and from an adapter's `cli_session_id`. References use explicit IDs rather than inferring relationships from filenames.

| Record | Required meaning |
|---|---|
| Session | Canonical repository identity, creation time, selected coordinator/configuration snapshot, ordered messages, current plan revision, linked run IDs, checkpoint references and delivery policy. It survives several approved execution rounds. |
| Message / turn | Stable message ID, author (`operator`, `coordinator`, or referenced worker), provenance, content hash, timestamp and source links. A turn groups the bounded coordination work responding to one operator submission, with durable step/time consumption across interruption and recovery. |
| Plan revision | Immutable revision ID and parent revision, goal/constraints, complete validated graph, pinned base/checkpoint, known context manifests and future-context selection rules, proposed routing candidates and limits, verification policy and a human-readable explanation of changes. Rejected/superseded revisions remain inspectable. |
| Approval | Operator action bound to the exact plan revision and digest, with an idempotency key and expected session revision. It never comes from a coordinator response or worker result. |
| Run / round | One frozen executable graph and configuration, linked to the approved revision and input snapshot. Existing node, attempt and run identities and limits retain their meanings. |
| ContextManifest | Exact recipient payload hash and retained payload, source identities/hashes/ranges, selection and summarization provenance, omissions, byte count and estimated token count. The inspector displays the payload actually delivered. |

The approval digest covers the canonical graph, repository/base or checkpoint identity, hashes of known imported outputs and existing context, resolved verification commands, effective permissions, authorized routing candidate/model policy, retry/fallback limits, concurrency and applicable session delivery policy. Future same-round outputs do not yet have hashes: approval binds their permitted source nodes, fields/artifact selections and deterministic rendering rules, together with the typed answer protocol. At each admission, materialize, hash, retain and validate the exact recipient manifest against those rules and use it in the attempt's input key. Dependency results and scope-preserving operator answers derived within that envelope do not require another graph approval; newly introduced sources or changes to bound inputs, selection rules, scope, commands, permissions or limits do. Any revised graph requires new operator approval. Revalidate the current revision and envelope before dispatch, and allow automatic retries/fallbacks only inside it. Selecting a different coordinator or modifying its effective configuration invalidates outstanding proposals and requires a new validated revision before dispatch.

Coordinator output is one versioned object (`schemas/coordinator-response.json`) whose `response` field is a closed discriminated union, validated before action: `ask_user` (revisioned typed questions), `request_context` (bounded repository-object or approved-artifact selections), `propose_plan` (complete graph proposal), `review` (evidence-linked findings and remaining questions), or `finish` (summary and delivery recommendation). Follow-up work is another `propose_plan`, not a spawn request. A response cannot execute shell commands, launch workers, approve a plan, change permission or verification policy, or select an unapproved repository. The application service validates references, graph structure, capability eligibility and limits independently of the model. One schema-repair follow-up is allowed; a second malformed response ends the turn with an actionable error.

Coordination settings are positive, explicitly validated values in the effective configuration: `max_steps` defaults to 6 and `max_duration_s` to 300 per turn. Each coordinator invocation or context-request action consumes one step; a schema-repair invocation consumes one step too, within the same total. Active processing time includes context assembly and managed calls but excludes operator wait; the duration limit cancels and drains an active call. `context_max_bytes` defaults to 131072 (128 KiB of UTF-8), with `context_max_estimated_tokens` defaulting to 16000. Both limits apply to the complete assembled input, including instructions and schema overhead; token counts are estimates, never exact provider billing. These are also the default per-recipient handoff limits; approved task context limits may be configured separately. Oversized required material blocks with a selection request instead of silent truncation; optional material can be omitted only with manifest entries. A continuation after an operator reply is a new turn with its own limits. Monetary ceilings remain phase 4 (section 8.5).

Context manifests select summaries, decisions, relevant files, pinned commits, artifact ranges, verification outcomes and user answers with provenance. Same-round `context.from` retains section 8.1's transitive-dependency restriction. Cross-round imports instead name a sealed checkpoint/source run, node and attempt, result/artifact hashes and output commit; the controller validates them separately before making them available as approved input. This is not completed-node cache reuse and cannot bypass section 6's reuse checks. A hash establishes identity, not the truth of a worker's assertions. Any generated summary names its input hashes and summarizer; uncertainty and omitted sources remain visible. Full agent logs and conversation transcripts are never automatic downstream context.

Session delivery policy retains the original delivery baseline, cumulative approved write scope and user-mandated checks across rounds. Only an explicit operator policy change can remove a required check or alter delivery scope; a coordinator's shortened follow-up graph cannot do so. The final delivery rules are in section 10.

---

## 6. Context exchange and run store

```
.agents/
  agents.yaml
  locks/                      # foreground owner files; native run/target locks from phase 2b
  sessions/<session-id>/      # phase 2c: session.json, messages, plans, approvals, checkpoints
    events.jsonl             # session events; run events remain in their owning run
  apply/<operation-id>/       # phase 4: durable plan, before-images, progress, conflict artifacts
  shadow-git/                 # phase 4: isolated objects/staging for non-git workspaces
  runs/
    <run-id>/
      run.json                 # graph, node states, config, input keys, final commit, budget ledger
      run.lock                 # phase 2b: diagnostic supervisor identity and heartbeat
      ipc.sock | (named pipe)  # supervisor endpoint (Unix socket file; on Windows the pipe has no file, only ipc.endpoint)
      ipc.endpoint             # endpoint address and protocol version
      ipc.token                # observer token (read-only commands); privileged commands use section 8.6 confirmation
      events.jsonl             # append-only event log (orchestrator-owned)
      routes.jsonl             # route log projection (section 4.4, orchestrator-owned)
      route.log                # route log, human-readable rendering of routes.jsonl
      PROJECT.spec.md          # snapshot used by this run
      01-n1-plan/
        task.spec.yaml
        result.spec.yaml
        attempts/NN/result.spec.yaml  # an earlier attempt's result, moved aside (section 8.2)
        input_answers.yaml     # operator answers for a needs_input node (relay reply / task_input)
        prompt.md              # exact rendered prompt sent to the CLI
        stdout.log  stderr.log
        artifacts/
      02-n2-research/ ...
      03-n3-implement-autosave/ ...
  worktrees/
    <run-id>/<node-id>/        # git worktree per node, including readers
```

Durable snapshots live in git, not in this tree: per-attempt output refs and merge refs under `refs/relay/<run>/...` (section 8.3), plus the integration branch `refs/heads/relay/<run>/integration` (section 8.3); they survive worktree pruning and are removed only by `relay clean --purge-runs`.

Rules:
- A worker writes only inside its own `NN-<node>/` directory (plus its worktree). The orchestrator writes the task and finalized result to that same directory, resolved through the node directory recorded in `run.json`; a raw worker result is replaced there after successful finalization. Inspection may fall back to a legacy `<node-id>/result.spec.yaml` only when the recorded directory has no result. Spec writes are atomic (temp file + rename).
- **Identifiers and records.** A run id is `<UTC date>-<4 hex digits>` (e.g. `2026-10-22-a1b2`), created by exclusive `mkdir` of the run directory (a collision draws a new suffix) and never reused: an id whose run directory exists is skipped, and from phase 2b so is one whose native lock object under `.agents/locks/` still exists even after its run directory was purged (the phase 1 to 2a interim lock file is removed on normal exit, so it gives no such protection; a purged id can recur, which is harmless because its refs and records were purged with it). Node directories are `NN-<node-id>` with `NN` the zero-padded creation order. `run.json` holds the run `state` (`running | done | failed | blocked | cancelled | interrupted`), `owner_generation`, the config hash, the graph, a record per node (`state` as in section 8.2, directory, blocked reason, attempt count, question revision, cumulative `active_duration_s`, attempt start/timeout and process-tracking markers for foreground recovery, `input_key`, `output_commit`), the final integration commit and the budget ledger (phase 4). Atomic writes flush the temp file to disk before renaming it; a leftover temp file from a crash is ignored and swept at the next open.
- **Session persistence (phase 2c).** Session IDs are unique path-safe IDs allocated exclusively and never reused. Atomic `session.json` records the section 5.6 identities, durable admission gate, current revision and approval-to-run intent; append-only messages, immutable plan/approval/context files and checkpoint manifests live beneath that directory. Coordinator-call records retain the turn/call ID, profile and input hashes, consumed limits, durable spawn/worker identity markers and completion outcome, so recovery cannot reset a turn's budget or overlook an old coordinator process. Run records gain optional session/revision links; older records without them remain valid. Session events reference run IDs and sequence cursors rather than copying run logs. Checkpoint commits are retained under `refs/relay-sessions/<session-id>/checkpoints/<checkpoint-id>`; manifests retain every contributing source ref and content hash. A ref cannot also be a directory of refs, so each contributing output is additionally retained under `refs/relay-sessions/<session-id>/checkpoint-sources/<checkpoint-id>/<node-id>`; a conflicted checkpoint has no combined ref, only these sources. Session refs use validated components and the same protected publication discipline as run refs.
- **Session ownership (phase 2c).** A cross-process native lifetime session lock under `.agents/locks/` covers planning, waiting for approval, dispatch, checkpointing and recovery, including intervals with no active run. Reuse the section 15 platform binding and verified filesystem semantics without requiring a detached supervisor, IPC or phase-2b run-lock integration. Keep the non-inheritable handle open for the owning service lifetime; lock objects live outside purgeable session directories and are never replaced or unlinked. Unsupported native session locking disables interactive ownership; legacy foreground headless execution retains its interim run-lock behavior. Acquire session ownership before run ownership and release in reverse order. Linked-run mutating commands from either CLI or TUI must acquire/honor session ownership and current revision, including resume, reply, cancel, apply and purge. Observer commands remain read-only. One logical owner writes session state and shares the active run's existing event writer; a second TUI may inspect durable records but cannot approve, answer or dispatch. After owner death, serialize intent/run recovery under the native session lock and prove old managed execution terminated before admission. Session ownership coordinates operators; it is not an isolation boundary against unrestricted same-user workers (section 9).
- **Session retention (phase 2c).** `clean` may decommission successful registered worktrees but must not purge runs, artifacts or refs referenced by a retained session/checkpoint, even when those runs are old. A confirmed session deletion first acquires session ownership, refuses active or unconfirmed execution, and removes the session's references and checkpoint refs; shared run references remain protected by other sessions. Deleting a session does not implicitly purge its runs. All reference updates and purge admission are serialized so cleanup cannot race a new checkpoint/import; recovery reconciles an interrupted deletion before admitting new work.
- **Foreground interim ownership (phases 1 and 2a; no supervisor):** the foreground coordinator is the single writer. It creates `.agents/locks/<repo-key>-<run-id>.lock` with exclusive-create semantics (`open(..., "x")`, i.e. `O_CREAT | O_EXCL`), writes PID and process-start identity into it, and removes it on normal exit. A second process that finds the file checks the recorded PID *and* process-start identity; a live owner means "run in progress, refuse". On Windows an exited process is dead even while its process handle remains queryable. In phase 1, a dead owner marks the single-node run `interrupted` and takeover through `relay run --retry`/`resume` re-runs it from its last durable state. Limitations, accepted for the MVP: no attach from a second front-end, and a PID-reuse/start-time race is possible in the interval between the identity check and takeover, which is acceptable because only one user-driven process starts a run. The native lifetime lock below replaces this for the phase 2b supervisor.
- **Foreground interruption recovery.** `relay resume RUN` takes the interim lock with explicit takeover intent, refusing a live or unknown owner. It can restart an interrupted single-node run from the recorded task/configuration and pinned commit, preserving the worker checkout and total attempt count. `relay run --retry RUN` explicitly retries a failed single-node run under the same limits; request, task and retry sources are mutually exclusive, and retry accepts only the recorded inputs plus an explicit total-attempt increase. A recovered unfinished attempt consumes its recorded attempt and active time. If owner death loses its completion timing, elapsed time from the durable start to recovery is charged conservatively, capped at the recorded per-attempt timeout. A new attempt uses a fresh CLI session and redoes capture and verification; partial publication is never treated as completion. A durably completed node with matching output/integration refs can finish the run without redispatch. Registered capture scratch is removed before recapture. Managed agent and verifier processes have durable spawn/identity markers; recovery terminates a confirmed old Unix process group, or proves that the Windows job has terminated, before dispatch. An incomplete spawn marker, an interrupted legacy attempt without process tracking, or an unverifiable process tree leaves the node blocked for intervention. Foreground Ctrl-C terminates the managed process tree and preserves an interrupted run for resume. This is whole-attempt recovery; supervisor generations and mid-publication reconciliation remain phase 2b.
- **Detached operation (phase 2b):** the per-run supervisor is the single writer of `run.json`, `events.jsonl`, `routes.jsonl` and `route.log`. Before binding IPC or changing run state, acquire an exclusive native lock on `.agents/locks/<repo-key>-<run-id>.lock`: `flock(LOCK_EX | LOCK_NB)` on supported Unix systems or `LockFileEx` with exclusive/nonblocking flags on Windows, called from the supervisor through the Python standard library (`fcntl.flock`) and `ctypes` (`LockFileEx`); no third-party native binding is used. Keep the lock handle open and non-inheritable until shutdown. A separate helper process that can outlive the supervisor is not an acceptable lock owner. `relay doctor` verifies support on the actual filesystem; unsupported platforms/filesystems fail before execution. Lock objects live outside purgeable run directories and `relay clean` never unlinks/replaces them; contenders must always lock the same object. Purge takes the corresponding run and target locks before deleting records, and run IDs are never reused.
- IPC endpoint existence and connection probes are discovery only, not ownership. A contender unable to acquire the native lock waits for readiness or attaches; failure to connect never authorizes unlinking a socket or launching workers. Only a lock holder may remove a leftover endpoint, bind a fresh endpoint, or recover state. This avoids the stale-socket unlink/rebind race between simultaneous starters. `run.lock` is replaceable diagnostic metadata (PID, process-start identity, generation, heartbeat), never the lock itself. Canonical repository identity plus run id namespaces endpoint addresses; shorten/hash Unix socket paths and Windows pipe names to fit platform limits.
- After owner death and OS-lock release, a replacement supervisor increments the owner generation, reconciles the durable event log and `run.json`, and terminates or proves termination of recorded old worker process groups before retrying them. Use process-start identity, not PID alone, to avoid PID reuse. If termination cannot be established, leave affected nodes `blocked` for intervention; never dispatch duplicate writers. Attempt results carry generation and attempt IDs; stale owners cannot publish results or update refs. Persist successful output refs and a durable completion event before releasing dependents; recovery finishes or rolls back an interrupted publication idempotently.
- Context handed to a node is assembled from `context.from` plus the pinned `PROJECT.spec.md`; phase-2c payload bounds, exact manifests and visible omissions follow section 5.6. Referenced files remain available only through the recipient's permitted snapshot/context access, not arbitrary host paths.
- Session continuity: for retries and follow-ups on the same agent the adapter stores `session.cli_session_id` and resumes; the per-adapter session id and resume flag are defined in section 7.3. Resume is only used within a node (retry), never across nodes, to keep nodes reproducible and independent. Legacy Gemini headless has no resume, so context is re-sent.
- Worktree retention and cleanup: Worktrees under `.agents/worktrees/<run-id>/<node-id>/` (or the short Windows layout of section 8.3) are preserved during node execution. Upon verified fan-in or final integration merge, worktrees of successful intermediate nodes are automatically decommissioned using `git worktree remove --force <path>` followed by `git worktree prune` (leaving commits and diff artifacts intact in the git object database under `refs/relay/<run>/...` and in the run store) to prevent disk space, inode, and git administrative lock exhaustion. Failed node worktrees are retained for debugging; retention is the caller's policy, and a node that fails is simply not decommissioned. Decommissioning has two explicit, trusted entry points and never removes anything else: `decommission(node_or_worktree)` removes only a registered worktree owned by the manager, resolved under its own root; `decommission_candidate(path)` is the caller-asserted path for the capture candidate worktree, which capture creates outside the root (section 8.3). Both refuse the main worktree and any unregistered or unrelated path, and are idempotent. Registration is read from NUL-delimited Git output and compared as normalized paths, so Windows separators and Git path quoting do not hide an owned worktree. Removal enables `core.longpaths` for the Git invocation, including failed-creation cleanup. If worktree creation fails part-way, the manager cleans up the just-created worktree, its administrative metadata, and any branch ref that the failed creation made. `relay clean [--older-than <duration>]` prunes old run worktrees.
- Completed-node reuse uses an `input_key` computed from a canonical encoding of the effective task spec, input commit and tree, all dependency output commits and result/artifact content hashes, the rendered context and `PROJECT.spec.md` snapshot, effective configuration (including verification commands and permissions), adapter/CLI versions, adapter execution fingerprint (below), and verifier version. Required files and commits must still exist and match their recorded hashes. Secrets and raw environments are excluded from stored keys. Changed inputs invalidate the node and its descendants; dependencies are recomputed in topological order before reuse is considered. CLI session resume is disabled when this key changes.
- **Adapter execution fingerprints.** Reuse compares an optional fingerprint for the completed node's actual adapter, not a newly selected route. The first supported capability is Claude using API-key authentication in the `--bare` mode described in section 7.2, and only with an explicit resolved model; the CLI-default model is uncomparable. Build the fingerprint from the exact effective child environment in the invocation after the same precedence rules used to launch it, the invocation command and mode, explicit model, and effective permission settings. For this Claude capability, the effective nonempty `ANTHROPIC_API_KEY` is the HMAC-SHA-256 key; the message is a canonical encoding of those inputs prefixed with the domain separator `relay.execution-fingerprint.v1\0`. Store only the resulting digest in the reuse key; never serialize the key or raw environment. An `apiKeyHelper`, OAuth/keychain or other authentication, alternate provider/helper settings, or an `agents.yaml` environment override other than `ANTHROPIC_API_KEY` makes the fingerprint uncomparable. Codex and other adapters remain uncomparable until a capability is separately verified; do not infer equivalence from undocumented auth files. Missing or uncomparable fingerprints prevent reuse and record reason `execution_fingerprint_uncomparable`. Treat this as a cache miss: keep prior results, artifacts and output refs inspectable. If the normal attempt limit blocks the required rerun, an explicit total-attempt increase through `relay resume --max-attempts N` admits it without discarding those retained outputs.
- Runtime state is never repository input or a deliverable. The **reserved paths** are `.git/**` and the runtime subtrees listed here (section 8.1 refers to this list). Explicitly exclude `.agents/locks/`, `.agents/runs/`, `.agents/sessions/` (phase 2c), `.agents/worktrees/`, `.agents/shadow-git/`, `.agents/scratch/`, `.agents/apply/`, and recorded legacy conflict artifacts from baseline creation, dirty checks, change capture, candidate materialization, and delivery. Do this in orchestrator path filters without editing the user's tracked ignore files. **Keeping the user's own git view clean:** in git mode, the first run idempotently adds a marked block (`# >>> relay runtime state` ... `# <<< relay runtime state`) listing those subtrees to the repository's local exclude file (the path from `git rev-parse --git-path info/exclude`, which is shared by linked worktrees and is never committed), so `git status` and `git add -A` in the user's checkout ignore runtime state and the nested worktrees are not offered for staging. `.gitignore` is never modified; `relay clean --purge-runs` leaves the block in place, and `relay doctor` verifies it. In shadow-git mode (phase 4), the isolated staging snapshot applies the same exclusions before its first commit. Validation fails if tracked files collide with a reserved runtime subtree. `.agents/agents.yaml` and human-owned project instructions remain ordinary inputs.

---

## 7. Adapters

### 7.1 Interface

```python
class AgentAdapter(Protocol):
    id: str  # "claude" | "codex" | ...

    async def probe(self) -> Probe: ...  # available, version?, levels: list[PermissionLevel], notes
    def build_invocation(
        self, task: TaskSpec, ctx: RunContext
    ) -> Invocation: ...  # cmd, args, env, cwd, stdin?
    async def parse_result(
        self, proc: ProcessResult, task: TaskSpec, ctx: RunContext
    ) -> ResultSpec: ...
    async def cancel(self, handle: WorkerHandle) -> None: ...  # kills the process tree
```

Adapters never decide routing or permissions; they only translate. They declare which permission levels they can enforce (section 9.3).

Process supervision terminates the whole worker tree on timeout. POSIX workers start in a new session: send `SIGTERM` to the process group, then escalate to `SIGKILL` even if the group leader has exited, because surviving descendants may still hold output pipes open. Windows workers are created suspended, assigned to a kill-on-close Job Object, then resumed; failed assignment or resume terminates the child before it can run unsupervised.

The `fake` adapter (tests only) drives `relay.adapters.fake_cli`, a stub agent process that follows a per-attempt scenario file: it writes, deletes and commits files, emits each output mode of section 7.3, returns `needs_input` with typed questions, fails, sleeps or hangs until killed, and records the prompt and environment it received. Scenarios are selected by node and attempt, the last one repeating, so retry, escalation, fallback and `needs_input` paths run without a real CLI.

### 7.2 Per-CLI mapping (non-interactive)

| | Claude Code | Codex | Gemini CLI | Cursor |
|---|---|---|---|---|
| Invocation | `claude -p "<prompt>"` | `codex exec "<prompt>"` (stdin supported; `-` reads the prompt from stdin) | `gemini -p "<prompt>"` (stdin appended) | `agent -p "<prompt>"` |
| Working dir | process cwd (`--add-dir` for extras) | `-C <dir>`, `--add-dir <dir>` | process cwd (`--include-directories`) | `--workspace <dir>`, `--add-dir <path>` |
| Model | `--model` (alias such as `opus`/`sonnet` or full name); `--effort low|medium|high|xhigh|max` | `-m`, `-c key=value` (reasoning-effort key **[verify]**; not shown by `--help`) | `-m` / `--model` (`auto`, `pro`, `flash`, `flash-lite` or a concrete name) | `--model` (bracket overrides such as `model[effort=high]`; `--list-models`) |
| Structured output | `--output-format json` + `--json-schema <schema>` (verified in 2.1.308: an inline JSON string, not a path, without a `$schema` key; the validated object is `structured_output`) | `--json` (JSONL events), `--output-schema <file>` (strict form required, section 5.4), `-o` / `--output-last-message <file>` | `--output-format` / `-o` `json` (response, stats, errors) or `stream-json` | `--output-format json|stream-json` (requires `-p`) |
| Session resume | `--resume <id>`; `--session-id <uuid>` pre-assigns an id; `--fork-session` | `codex exec resume <id>` / `--last`; `codex exec fork` | `--resume` / `-r` takes `latest` or an index; headless support **[verify]**, so treated as stateless | `--resume [chatId]`, `--continue` |
| Full permission (default) | `--dangerously-skip-permissions` (or `--permission-mode bypassPermissions`) | `--dangerously-bypass-approvals-and-sandbox` (the `--yolo` alias is not listed by `codex exec --help` in 0.162.1; use the long form) | `--approval-mode=yolo` (`--yolo`/`-y` is deprecated) | `--force --trust` (`--yolo` is a documented alias of `--force`) |
| Workspace-limited | Needs a verified profile (section 9.3); `acceptEdits` + `--allowedTools` alone is not one. `--restricted` removes shell tools and confines file tools to the working directories, so it is not `workspace` (no build/test) | `-s workspace-write`, subject to section 9.3 checks | Needs a verified profile (section 9.3); `auto_edit` alone is not one. `-s`/`--sandbox` (boolean) exists **[verify enforcement]** | Candidate: `--sandbox enabled` **[verify enforcement]**; unsupported until verified by a canary |
| Read-only | `--permission-mode plan` (alternatively `--restricted` with only read tools via `--tools`) | `-s read-only` | `--approval-mode=plan` or `default` (headless denies "ask") | `--mode plan` (documented as read-only, no edits) without `--force`; stdin closed/null to prevent interactive hangs |
| Isolation / reproducibility | `--bare` (skips hooks/CLAUDE.md discovery; context passed explicitly), only when API-key auth is configured: under `--bare`, Anthropic auth is strictly `ANTHROPIC_API_KEY` or `apiKeyHelper` via `--settings`, and OAuth/keychain (subscription) login is never read; `probe()` records the auth mode and the adapter omits `--bare` for subscription logins. `--no-session-persistence` | `--ephemeral` (no persisted session), `--ignore-user-config`, `--ignore-rules`, `--skip-git-repo-check` | n/a | `-w/--worktree [name]` and `--worktree-base` (router prefers its own worktree) |
| Budget cap | `--max-budget-usd <amount>` (print mode only); possible basis for hard cost mode, section 8.5 **[verify granularity]** | none found | none found | none found |

Codex `codex exec --help` also lists `--worktree`; the router never uses it, to keep one worktree owner. The `-a/--ask-for-approval` flag exists only on the top-level `codex` command, not on `exec`, so non-interactive approval behaviour on `exec` is set through the sandbox/bypass flags and `-c approval_policy=...` **[verify]**. The Codex `-p` flag selects a config profile, not a prompt. The adapter does not pass `--ephemeral`, because it disables the persisted session that in-node resume depends on, nor `--ignore-user-config` or `--ignore-rules` by default, because they can discard provider and auth configuration; `--skip-git-repo-check` is unnecessary because every node worktree is a git repository.

The restricted-level entries are invocation hints; what counts as enforcement, and which levels `probe()` advertises, is defined in section 9.3.

Known caveats (tracked as risks): Codex approval-flag behaviour and default sandbox for `exec` differ between third-party sources, so `relay doctor` runs a canary command and records actual behaviour; MCP calls under `codex exec -s read-only` are reported as cancelled; Gemini headless auto-denies anything that would prompt unless yolo or a policy allows it, and its `--allowed-tools` flag is deprecated in favour of the Policy Engine; the Gemini docs also state that Gemini CLI was replaced by Antigravity CLI for unpaid-tier and Google One users on 2026-06-18, so availability depends on the user's plan; Cursor runs unattended with an account login (verified; the login lives in the user profile, so the Windows environment allowlist must keep `USERPROFILE` and `APPDATA`) or with `CURSOR_API_KEY` / `--api-key`; its binary name is `agent` with `cursor-agent` as a legacy alias.

**Antigravity CLI (`agy`).** `agy` is the Antigravity CLI, a Go single binary and Google's current terminal surface of the Antigravity 2.0 agent harness; it replaced Gemini CLI for unpaid-tier and Google One users on 2026-06-18, while legacy Gemini CLI remains for some paid and enterprise plans, so the Gemini column above describes those legacy installs. The binary installs at `~/.local/bin/agy` (macOS/Linux) and `%LOCALAPPDATA%\agy\bin` (Windows, `agy.exe`); no `.cmd` shim is reported, so ordinary PATH/PATHEXT resolution applies. Headless runs use `agy -p "<prompt>"` (aliases `--print`, `--prompt`); `--output-format text|json|stream-json`, `--input-format text|stream-json` (`stream-json` input requires `stream-json` output), `--model <slug>` (slugs from `agy models`, e.g. `gemini-4-argon-high`, `gemini-3.8-flash-medium`), `--effort low|medium|high`, `--agent <name>` (list with `agy agents`), `--add-dir`, `--mode accept-edits|auto|plan`, `--sandbox`, `--dangerously-skip-permissions`, `--print-timeout <duration>`, `--log-file`, `--project`. The JSON envelope carries `conversation_id`, `status` (`SUCCESS`, `ERROR`, `CANCELED`, `INTERRUPTED`, `INVALID`, `WAITING`, `RUNNING`), `response`, `error`, `duration_seconds`, `num_turns`, `structured_output` (present with `--json-schema`) and `usage` (`input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_tokens`, `total_tokens`); exit codes are 0 success, 1 error, 2 protocol/control error, and `stream-json` emits `init`, `step_update` and a terminal `result` of the same envelope shape. `--json-schema` accepts an inline schema string, a `.json` file path, or a primitive type name, and the root must be an object. Session resume is `--conversation <conversation_id>` (or `-c`/`--continue` for the most recent conversation), with context retained across resume **[verify]**. In headless mode with no bypass, tools that would prompt are soft-denied, the run continues and exits 0 with a notice on stderr; `--dangerously-skip-permissions` auto-approves all tools (maps to `full`), and `--mode plan` is research/outline mode — whether it prevents all repository mutation is unverified **[verify]**, so it cannot be advertised as `readonly`. `workspace` and `readonly` stay unadvertised until a canary proves subprocess and file confinement, and `--sandbox` enables the terminal sandbox with containment scope unverified **[verify]**. Auth is system-keyring sign-in (interactive once) or `GEMINI_API_KEY` for CI; settings live at `~/.gemini/antigravity-cli/settings.json` with no documented config-home environment override **[verify]**. `-p` takes the prompt as an argv argument and the docs do not clearly document a plain-text stdin prompt mode, so the adapter passes the prompt on argv initially and a long-prompt canary covers length limits **[verify]**. `thinking_tokens` and `cache_read_tokens` are reported separately and how thinking tokens are billed is undocumented **[verify]**, so cost is labeled `estimated`/`unknown` until the pricing basis is confirmed (section 8.5).

**Windows process spawning:** `resolveCommand()` resolves explicit paths and PATH/PATHEXT entries. Native executables are launched directly. `.cmd` and `.bat` shims use a raw `cmd.exe /d /s /v:off /c` command line, with shell metacharacters escaped for both the outer invocation and the shim's `%*` forwarding pass; Windows native-argument quoting alone is insufficient. Delayed expansion is disabled. Batch arguments containing CR, LF or NUL are rejected; prompts with line breaks use stdin or a file where the CLI supports it (Codex `-`, Claude and Gemini piped stdin). A prose-only permission probe for an argv-based CLI uses a single-line prompt. PowerShell-only `.ps1` shims are unsupported. `relay doctor` checks quote and newline round-trips through the supported prompt channel.

### 7.3 Output parsing order
1. Native structured output (schema-constrained) if the CLI supports it.
2. `result.spec.yaml` written by the agent in the node dir.
3. JSON/YAML block extracted from stdout.
4. Otherwise a synthesized candidate result: provisional `status` from exit code, `summary` from the last message, `changes` from the complete change capture in section 8.3 (always computed by the orchestrator, never trusted from the agent). Exit code zero alone does not finalize a write node as `done`.

**Parsing trust boundary:** the orchestrator owns node identity and operational fields. Agent-authored `id` and `agent` are always overwritten with the true node id and adapter id. Agent-authored `model`, `session` and `usage` in a structured result are discarded before validation, so a malformed value cannot fail the attempt, and replaced from the CLI envelope. When the CLI reports no model and routing used the `default` sentinel, `result.model` is null, and cost settlement labels that cost `unknown` (section 8.5).

In all parsing modes, the adapter extracts and records `session.cli_session_id` from the CLI output payload to enable in-node session resumption on retries. Field names: Claude JSON `session_id`; Codex JSONL `thread.started` event `thread_id` (both confirmed against real output; `--resume` and `codex exec resume` keep the same id and the earlier context); Cursor JSON `session_id` (confirmed on `agent` 2026.10.19, same behaviour on `--resume`); Antigravity JSON `conversation_id`, resumed with `--conversation <conversation_id>` (or `-c`/`--continue` for the most recent) with context retention **[verify]**; legacy Gemini none. `relay doctor` runs a canary per adapter, checks that a session id is extracted and that a follow-up resume with it succeeds, and records the observed field names in the adapter profile. Resume is confirmed only on exact equality with a token planted in the first prompt; a follow-up that returns different context is `inconclusive`, not proof that resume failed (the surrounding unverified-session risks are in section 17). If extraction fails, retries run in a fresh session with the prior attempt's captured state passed in the prompt (the same behaviour as legacy Gemini).

### 7.4 Coordinator and progress capabilities (phase 2c)

The first interactive workers are Claude and Codex. A separately selected coordinator may use any CLI that has a qualified profile (at present only Claude is a candidate; Codex failed spike 2.31) through the user's existing authentication; no provider API, new credential store or billing migration is introduced. The coordinator defaults to the logical `balanced` model tier resolved through the selected agent's configured catalog, and to Claude only when its coordinator capability qualifies. Otherwise ask the operator to select a qualifying coordinator explicitly; do not silently switch providers. Show the resolved model and fail clearly if the requested tier cannot resolve. An unavailable coordinator does not prevent inspection of saved sessions or use of existing headless commands.

Adapters may expose optional `coordinate` and `progress` capabilities in addition to the worker interface. `coordinate` consumes the bounded input and closed response schema in section 5.6, with invocation construction, process supervision and result validation shared with existing adapter infrastructure. Its schema mode is explicit: the worker `ResultSpec` remains the worker contract, not a container for unchecked planner JSON. Store model identity, effective profile, schema version and CLI version with each call. Coordinator sessions are separate from worker CLI sessions; continuity is optional and never substitutes for the pinned manifest. A changed input/profile or unsupported resume uses a fresh CLI session.

**Restricted planning is mandatory.** A qualifying profile is tool-free or enforces read-only inspection under section 9.3, and must also enforce the context boundary in sections 5.6 and 9.4. Prefer tool-free calls with Relay-mediated `request_context` actions. A read-only tool path qualifies only if every model-visible read is restricted to approved sources and provides equivalent bounded, provenance-observable capture into the recipient manifest; unrestricted filesystem reads or unobserved tool context do not qualify merely because writes are denied. Qualification is specific to CLI version, OS, authentication mode and effective configuration, including startup hooks, MCP/apps, managed policy and enabled helpers. A tool-free profile must disable model tool execution and exclude unapproved executable startup/customization paths; the permitted authentication transport/state exceptions are explicit. An empty tool list alone, a disposable checkout, or a promise in a prompt is insufficient. Capability checks must not read or copy private authentication material into Relay records. An unknown, changed or unsupported profile makes coordination unavailable; there is no `full` fallback. Qualification requires opt-in canaries and review, not a successful version probe or offline flag fixture.

Spike 2.31 (Windows 11, single observations) found the Claude Code 2.1.310 combination `--tools ""`, `--strict-mcp-config` with an empty `--mcp-config`, `--disable-slash-commands`, `--setting-sources ""` and `--no-session-persistence` to be a viable tool-free candidate, and Codex 0.164.0 `-s read-only` to fail the context boundary because it reads files outside the workspace; neither is a qualification record. Official CLI flags are candidate mechanisms, not verified Relay profiles **[verify]**. Claude's documented tool controls distinguish built-in tools from MCP; `--bare` suppresses discovery but does not use OAuth/keychain credentials and therefore cannot be imposed on subscription users. Managed hooks/configuration also need evaluation when considering other restricted modes. See the [Claude CLI reference](https://code.claude.com/docs/en/cli-reference) and [headless authentication guidance](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode). Codex's sandbox and configuration controls require the same version/OS/authentication qualification. Do not advertise any candidate as enforced until the complete profile passes. Existing worker permissions and authentication behavior remain governed by sections 7.2 and 9.

`progress` (implemented for Codex `--json` and, opt-in, Claude `--output-format stream-json --verbose`; Claude streaming with `--json-schema` was observed to yield the final validated result on 2.1.310, a single observation) normalizes only supported public CLI output into bounded text/tool-status updates with attempt/call identity. Structured-output parsing and streaming mode must qualify together; a streaming invocation must still yield the final validated result. Unknown event types remain inspectable diagnostics and cannot trigger control actions. Unsupported streaming shows lifecycle phases and final output honestly. Do not embed native agent TUIs, emulate interactive PTYs, claim arbitrary mid-tool steering/approval, or require an ACP migration. Buffering, backpressure and rendering rules are in section 11.

---

## 8. Execution model: foreground owners and optional supervisor

The execution owner starts several workers according to a task DAG. The owner is a foreground runner or SessionController, or a detached supervisor when that mode is enabled; all use the same routing and scheduling rules.

### 8.1 Task graph

Validate the complete graph before dispatch and after every proposed mutation: unique path-safe IDs (which may not be a reserved ref component, section 8.3), known categories, existing dependency and context references, no self-dependencies or cycles, and bounded node/depth counts. `context.from` may reference only transitive dependencies. Task specs, `owns` and `diff_scope` must not target or intersect the reserved paths defined in section 6 (`.git/**` and the runtime subtrees); such tasks are rejected during graph validation. The check gives the same answer on every host and errs toward rejecting. It compares path segments after dropping empty and `.` segments, ignores case, and lets `**` stand for any number of segments. A segment matches a reserved name if it could refer to it on Windows: as a glob, with trailing dots or spaces, or with `~` followed by a digit or glob character (a possible 8.3 short name such as `GIT~1`) or a `:` (a possible NTFS stream). A scope is rejected when it can reach a reserved path through at least one segment other than `*` or `**`; scopes made only of those, such as `**` or `*/hooks/*`, cover runtime state only by accident, and the orchestrator filters it out of them (section 6). A path that names the repository root itself (`.`, `./`) is rejected. `.agents/agents.yaml` and project instructions are ordinary repository files and may appear in `owns`/`diff_scope`; a node that edits `.agents/agents.yaml` changes only the delivered result, never the running run (the configuration is snapshotted at run start, section 3). Resolve node access explicitly as `read|write`: review, debug, architecture and codebase research default to `read`; other categories default to `write`. A `needs_input` result blocks the node and its dependents (protocol in section 8.2).

Graph dependency depth counts edges from a root (depth zero) and must not exceed `policy.max_depth`. Each node may specify integer `priority` (default zero; higher first, node id breaks ties) and `routing` using the task routing fields in section 5.2, including `routing.agent` to pin a worker. Graph node IDs must also be unique ignoring case for portable worktree and ref names. `graph-verification` and IDs beginning with `resolve-` are reserved for orchestrator operations.

```yaml
spec_version: 1
run_id: auto
policy: { on_failure: continue-independent, max_depth: 4, max_nodes: 20, cost_ceiling_usd: 15, cost_mode: advisory }  # cost_* fields: phase 4; rejected as unsupported before then
verify: default
commands: { build: "npm run build", test: "npm test", lint: "npm run lint" }
integration_acceptance:
  - { run: "npm test", expect_exit: 0 }
nodes:
  - { id: n1-research,  category: codebase-research, goal: "Map autosave call sites" }
  - { id: n2-impl-a,    category: implement, goal: "Debounce autosave",   depends_on: [n1-research], owns: ["src/editor/**"] }
  - { id: n3-impl-b,    category: implement, goal: "Add retry to uploader", depends_on: [n1-research], owns: ["src/net/**"] }
  - { id: n4-tests,     category: test,      goal: "Cover both changes",    depends_on: [n2-impl-a, n3-impl-b] }
  - { id: n5-review,    category: review,    goal: "Review merged diff",    depends_on: [n4-tests] }
```

```
            ┌─▶ n2-impl-a (codex)  ─┐
n1-research ┤                        ├─▶ n4-tests ─▶ n5-review (claude deep)
 (gemini)   └─▶ n3-impl-b (claude) ─┘        (merge + integration verify)
```

### 8.2 Scheduling
- Ready nodes (all dependencies `done`) are queued by priority and run in a bounded pool: global `defaults.concurrency` and per-agent `concurrency` (rate limits, licences, cost).
- Per-node timeout and `max_attempts`; in phase 1 a retry stays on the originally routed agent. Model escalation, cross-agent fallback and timeout policy (including retrying after a timeout) are phase 2a. Every retry goes back through the Router before it starts and writes a `routed` route-log entry carrying its attempt number; phase 1 has no separate `retried` event. A retry that cannot be routed is recorded as `rejected` and ends the node. `escalated` and `fallback` entries arrive with the phase-2a features (section 4.4).
- Failure policy per graph: `fail-fast` stops dispatch and cancels active attempts after a terminal failure; `continue-independent` (default) finishes unaffected branches and transitively blocks dependents of unsuccessful nodes. `continue-all` is reserved and rejected in v1: running a dependent without a successful input snapshot would violate the dependency contract. Conflict-resolution failures follow the selected policy rather than unconditionally stopping independent work.
- Cancellation (`relay cancel` or OpenCode abort) sends a kill to the whole process tree of each running worker, marks nodes `cancelled`, and leaves worktrees for inspection.
- States: `pending | running | done | failed | blocked | cancelled`, persisted in `run.json`; `relay resume <run>` reconciles interrupted attempts, then reuses `done` nodes only when their complete `input_key` and stored outputs still match (section 6). Invalidated descendants return to `pending`; independent unchanged nodes can be reused. Outstanding cost reservations survive restart (section 8.5).
- `max_attempts` is a total per-node limit across all agents, escalations, and fallbacks; it does not reset on agent switch or resume. When `budget.max_attempts` is unset, phase 1 runs exactly one attempt. A schema-repair follow-up is limited to one per attempt and is charged to that attempt. Each attempt persists its phase (`agent`, `capture`, `verify`, `publish`) so a timeout or crash during verification cannot be mistaken for success. Before building an attempt's invocation, the orchestrator moves any `result.spec.yaml` left in the node directory to `attempts/NN/` (`NN` the attempt before the one starting; a file already archived there is never overwritten, the newer one gets a numbered name beside it), for every adapter: the parsing order of section 7.3 then reads only the current attempt's output, and earlier results, including a `needs_input` question set, stay inspectable. Node cancellation blocks its descendants; run cancellation also marks pending nodes `cancelled`. A quiescent run with blocked nodes is `blocked`, not successful; final integration is published as complete only when all required nodes are `done`. Partial outputs remain inspectable.
- **`needs_input` protocol.** An agent that needs operator clarification returns `status: needs_input` with structured queries in `open_questions`. The orchestrator (the foreground runner, phase-2c SessionController, or phase-2b supervisor) sets the node `blocked` (reason `needs_input`), preserves the questions in `result.spec.yaml`, publishes no successful output, and blocks dependents until the operator answers via `relay reply <run> <node> "<answers>"` or the OpenCode `task_input` tool; active phase-2c answers use the owner mailbox in section 8.7. Answers are persisted to `input_answers.yaml` in the node directory and rendered into the next attempt's prompt as operator input; input submitted through a model tool is labeled host-submitted content unless the host independently attests operator authorship, and is never elevated to trusted operator policy. A `needs_input` attempt still consumes an attempt and its actual duration/cost. Accept answers only for the current `needs_input` question set, using stable question IDs, an orchestrator-assigned question-set revision that increases per new request, and an idempotent submission ID. Persist answers append-only with operator provenance and include their content hash/revision in the effective task input key. Duplicate submissions have no effect; stale revisions are rejected. After recording answers, enqueue only if attempt, execution-time and monetary limits permit; otherwise keep the node `blocked` with the specific exhausted-limit reason and preserve its answers. The operator can explicitly increase the total limit via `relay resume --max-attempts N` (never an implicit reset). Time waiting for operator answers is excluded from cumulative active execution duration. A fresh CLI session is required when answers change the input key; preserve the node's prior worktree edits, but never reuse an old successful output under the new key.
- **`needs_input` in phase 1 (foreground mode, no supervisor or IPC).** This feature is prioritized into the MVP because unattended agents otherwise guess at ambiguous requirements. The adapter prompt tells the agent to stop and return `status: needs_input` with typed questions (`open_questions`, schema in section 5.3) when it cannot resolve a decision from the repository, rather than guessing; free-text questions inside a `done` result remain advisory, so the feature depends on the agent following the result-spec protocol (canary in `relay doctor`). When a node returns `needs_input`:
  1. **Interactive terminal** (stdin and stdout are a TTY and `--non-interactive` is not set): `relay run` prints the questions (id, text, options, default), reads the answers inline, records them (provenance `operator-tty`, a fresh `submission_id`, the current `question_revision`), and starts the next attempt in the same process. Time spent waiting is excluded from the node's active duration. The operator may answer `:abort` to cancel the run (exit code 4) or press Enter on a question with a default to accept it.
  2. **Non-interactive** (CI, pipes, `--non-interactive`, or the operator declines to answer): the run is persisted as `blocked` (reason `needs_input`), the foreground process releases its lock and exits with code 6, and prints the exact `relay reply <run> <node> ...` command. `relay reply` is privileged (CLI confirmation or `--yes`, section 8.6), records the answers append-only with provenance `operator-cli`, and with `--resume` (or a following `relay resume <run>`) re-runs the node in the foreground, taking the interim run lock (section 6). Reply and resume are file-based in this phase: no IPC is involved.
  3. **Shared rules from the protocol above apply unchanged:** the blocked attempt counts against `max_attempts` and cost; duplicate submissions are idempotent and stale revisions are rejected; answers change the `input_key` and force a fresh CLI session while preserving the node's worktree edits; if limits are exhausted the node stays `blocked` with the exhausted-limit reason until `relay resume --max-attempts N`.
  4. **Not in phase 1:** replies routed to a detached supervisor, graph-level blocking of dependents (a phase 1 run has one node), the OpenCode `task_input` tool, and `wait` semantics; they arrive with phase 2b and phase 3 and reuse the same answer files and event types.

### 8.3 Isolation and merging for parallel writers
- Scheduling uses each writer's effective allowed write scope: explicit `diff_scope`, otherwise `owns`, otherwise the entire repository. Two writers may run concurrently only if those scopes are provably disjoint; an unknown glob intersection is treated as overlap. Broader `diff_scope` therefore broadens scheduling reservations even when `owns` is narrower. This prevents undeclared/generated secondary files from bypassing overlap detection.
- At run creation, resolve and pin the base commit. For ordinary clean runs, this is the current target branch HEAD. When `--allow-dirty` is enabled, the orchestrator creates a synthetic base commit (`refs/relay/<run>/base`) whose tree captures the initial tracked and untracked user snapshot and whose parent is the checked-out branch HEAD. Because that snapshot copies untracked, non-ignored files into git objects (and into every node's worktree), the orchestrator first scans the untracked set for sensitive-looking paths (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `*.pfx`, `credentials*`, `*secret*`) and lists matches in the run-start confirmation; the user can proceed, or exclude them with `--exclude <glob>` (repeatable; excluded paths are absent from the snapshot and from every worktree, and are never delivered by `apply`). Refs under `refs/relay/` are never pushed by `relay`, and `relay clean --purge-runs` deletes them; the matches (paths only, never contents) are recorded in the run start event. Every node receives an immutable `input_commit`: the base commit (or synthetic base commit under `--allow-dirty`) for roots, its dependency's `output_commit` for a single dependency, or a deterministic merge of dependency outputs for fan-in. Dependencies are merged in stable topological order with node id as the tie-breaker; ancestor commits are included only once. No node implicitly reads a moving branch tip.
- Each write-capable node gets its own `git worktree` at its input commit. In phase 1 the worktree is detached at that commit (no branch): the orchestrator publishes the node's output under the `refs/relay/<run>/<node>` ref (below), so no `relay/<run>/<node>` branch is created and the user's branches are never touched. Node worktrees remain detached in phases 1 and 2a; a branch-backed node worktree on `refs/heads/relay/<run>/<node>` is not used. Effective write scopes (including `owns` where applicable) are checked at planning time; overlapping scopes are serialized or reported. Serialization alone does not propagate edits: a later node needing an earlier node's changes must have an explicit dependency. When the input commit records a `.gitmodules` file, the worktree manager runs `git submodule update --init --recursive` before invoking the adapter, scoped with `-c protocol.file.allow=always` so a local-path submodule passes the file-transport check; a submodule failure is a hard error that fails worktree creation. Git LFS is best-effort: only when `git-lfs` is installed and the worktree's `.gitattributes` declares `filter=lfs` does the manager run `git lfs pull`, and an absent `git-lfs` or a failed pull is recorded rather than treated as a run failure.
- **Path length (Windows):** worktrees nested under the repository add roughly 55 characters before any project depth, and tools such as `node_modules` trees exceed the 260-character limit quickly. On Windows `paths.worktree_root: auto` therefore resolves to a short root outside the repository (`%LOCALAPPDATA%\relay\wt\<repo-hash>\<run-short>\<node-short>`, where `<repo-hash>` is the first 8 hex characters of the repository path's SHA-256, and `<run-short>`/`<node-short>` truncate the run id to 8 and the node id to 16 characters, each appending a 4-hex hash of the full id when truncated so two ids sharing a long prefix never collide); the full ids stay in `run.json`. Worktrees are created with `core.longpaths=true` set per worktree, never in the user's global git config; enabling a per-worktree value requires the repo-local `extensions.worktreeConfig` switch, which the manager turns on idempotently in the repository's shared config, so the main worktree is untouched beyond that one switch. `relay doctor` reports Windows `LongPathsEnabled`, creates and removes a path of 300+ characters in the chosen root, and fails early with the remedy (enable long paths, or set a shorter `paths.worktree_root`). On other platforms `auto` is `.agents/worktrees`, and an explicit absolute path may be used anywhere.
- Shared package/build cache contention: concurrent commands (`npm install`, `cargo build`, etc.) across parallel worktrees sharing user cache directories (e.g. `~/.npm`, `~/.cargo`) can race on file locks. The worktree manager isolates local build-target caches that hold no credentials — it points `CARGO_TARGET_DIR` at `.agents/scratch/<run>/<node>/target` — and serializes cache-mutating build phases when cache contention risks are declared in `PROJECT.spec.md`. Credential-bearing homes (`CARGO_HOME`, `RUSTUP_HOME`, etc.) are passed through, never isolated (section 9.4).
- Read-only nodes (`read` access: research, review, debug, architecture; section 8.1) get a detached checkout at the same dependency-derived input commit, with `readonly` enforcement. They never run against the main working tree. A reader's `output_commit` equals its `input_commit`, so code snapshots propagate through review/research chains. Result specs and artifacts are captured through the node output channel, outside the repository snapshot.
- **Change capture and commit ownership:** agents are asked to leave edits uncommitted. Once the worker exits, the orchestrator resolves `input_commit` to a canonical sha and captures the complete delta from it, including agent-created commits, staged and unstaged edits, untracked non-ignored files, deletions, renames, binary data, and file modes. Ignored outputs are excluded unless explicitly declared as deliverables; reserved runtime paths (section 6) never enter the delta. It materializes that delta in a separate candidate checkout — a detached worktree at a floating candidate commit — enforces scope, and runs verification there. The floating commit has no ref; publication builds the output commit from the captured candidate tree, not from that floating commit. Agent-reported file lists and commit SHAs are informational only.
- On successful verification of an ordinary write node, the orchestrator creates the output commit — authored and committed by the orchestrator identity (never the agent), with a deterministic message and pinned timestamps, so the same input commit, candidate tree and message always yield the same sha — whose parent is the node's canonical `input_commit` and whose tree is the captured candidate tree, then points the output ref `refs/relay/<run>/<node>` at it and records `output_commit`, the diff artifact, and verification evidence before marking the node `done`. No-change nodes reuse the input commit (the output ref points at `input_commit`). The diff artifact is written at the fixed node path `artifacts/diff.patch` with a deterministic pinned diff configuration, so the same two commits always produce byte-identical output. Verification-generated outputs do not enter the deliverable; changes to tracked candidate files during verification fail the attempt. Failed attempts retain their worktree and artifacts for inspection but publish no successful output commit. Retries may continue those edits within the same node and input snapshot; fallback receives the captured attempt state explicitly.
- **Output refs and publication guards.** The node's current output lives at `refs/relay/<run>/<node>`, and each successful attempt also gets an immutable per-attempt ref `refs/relay/<run>/<node>@<attempt>` so pruning and garbage collection never lose a prior attempt. Because the output commit's parent is always `input_commit`, a retry's output is a *sibling* (never a descendant) of the prior attempt; the current ref advances to a new attempt's output only when the attempt is supplied and the prior ref is a prior output for the same input. Republishing the same attempt with the same tree is idempotent and returns the same sha; the same attempt with different content is refused. Every ref update is a compare-and-swap against the value observed before any write, so a concurrent writer cannot be raced past a read: a lost current-ref compare-and-swap leaves the current ref and any pre-existing attempt ref unchanged and rolls back only an attempt ref this call created. A same-attempt republish never moves the current ref backward from a later attempt. Before any ref is read or written the node id and run id are validated as single path-safe segments, and `base` and `integration` are reserved ref components (matched case-insensitively) so a node id can never collide with the synthetic base ref `refs/relay/<run>/base` or the integration branch. Publication is refused unless verification allowed it (section 10 item 5), and a candidate tree that still contains a reserved runtime path (section 6) is refused, never silently stripped. The user's HEAD and branches are never touched.
- Merge nodes are implicit before fan-in. On conflict, create a `resolve-conflict` task with the exact merge inputs and unresolved paths, subject to the same permission, budget, scope, capture, and verification rules as other writers, bounded to `max_attempts: 1`. If automated conflict resolution fails, the fan-in node is marked `failed`; failure policy determines whether independent work continues. Its verified resolution becomes the merge output. Run the graph's integration acceptance commands on every merged snapshot before releasing dependents.
- Completion also merges the successful node outputs into the run's integration branch `refs/heads/relay/<run>/integration` (a real branch; output and attempt refs remain under `refs/relay/...`) and verifies that final tree, even when no explicit fan-in exists. Only successful node outputs are merged: a failed or refused node is absent. They are merged onto the pinned base in deterministic ascending node-id order, fast-forwarding when the incoming commit already contains the accumulated history, a no-op when it is already contained, and otherwise a real merge commit with both contributing outputs as parents (never flattened to a single parent). A merge conflict raises before the branch is written, leaving an existing integration branch untouched; the branch is never moved backward or to a commit that loses already-integrated work, and every update is a compare-and-swap. Duplicate node ids in one integration call are rejected. Rebuilding the branch after a changed or omitted output is refused rather than losing prior integrated work; foreground graph resume (phase 2a) reuses or recomputes outputs and updates the integration branch only through the compare-and-swap rules above. `run.json` and CLI/plugin results expose the branch, its commit, and the diff from the pinned base. The user's checked-out branch is not advanced automatically. To apply verified deliverables to the active working tree or branch, the user runs `relay apply <run>` (or OpenCode `task_apply`, section 13.1), a privileged command that needs user confirmation (section 8.6). Behaviour:
  - **Git mode:** pin the target repository, branch and current target commit, then prepare a fast-forward or merge with the run's integration commit in a disposable checkout. Conflicts abort preparation without touching the user's checkout; do not start an in-place merge there. Run the run's recorded integration acceptance commands (or its graph `build`/`test`/`lint` defaults) on the exact prepared tree, since target-branch edits made after the run can introduce regressions even in a clean textual merge. Verification always runs against that tree when commands are recorded; earlier evidence is not reused. When the run recorded no integration commands there is nothing authorized to run and the outcome is reported as not run; a run whose graph opted out with `verify: none` is reported as skipped with its recorded reason. Verification failure rejects application; `--force` is itself rejected in git mode (section 14).
  - Compute the target-to-prepared-tree delta, including both old and new rename paths and directory/file collisions. When the target is the checked-out branch, compare it with staged, unstaged and untracked user edits; disjoint dirty edits are preserved, while overlap aborts with a path list. For `--allow-dirty` runs, user working-tree edits whose contents and modes match the captured baseline snapshot tree are recognized as pre-existing base state rather than concurrent conflicting edits, allowing safe delivery. A matching working file does not excuse separately staged edits: a changed index entry must also match the baseline to qualify for that exception. A new or non-current branch target does not install into the current checkout, so that checkout's dirty paths do not block preparation. Recheck target ref, index identity, file contents and modes immediately before installation. A changed target invalidates preparation and confirmation. `--branch B` selects the target branch: an existing local branch is updated from its current tip, and a missing branch is created from the current `HEAD` (a concurrently created branch is refused). The command never switches the checkout, so a new or non-current target leaves the current index and working tree untouched. For `--allow-dirty` runs, captured user changes are reported explicitly as part of the proposed delivery.
  - Confirmation is required unless `--yes`; a non-TTY session without `--yes`, or a declined prompt, exits 7 and changes nothing. After the expected-ref update succeeds and only when the target is the checked-out branch, the delta's paths are restored from the prepared commit into the index and working tree; non-overlapping staged, unstaged and untracked user edits are preserved.
  - **Shadow-git mode (phase 4):** keep the run's original baseline immutable. Prepare a per-path three-way comparison of baseline, current workspace and integration output; include additions, deletions, both rename paths, file types and modes. A path already equal to the desired output is a no-op. Unchanged user paths may be updated, and unrelated user edits are preserved. Conflicting paths abort the entire application before writes by default. Store conflict manifests and incoming contents under a uniquely created `.agents/apply/<operation-id>/conflicts/`, including deletion markers and baseline/current/desired hashes; never overwrite a sibling `<path>.relay-incoming` file. Explicit `--force` authorizes only the listed conflicting replacements in the confirmed plan and retains their before-images; it does not bypass containment, verification, or concurrent-edit checks.
  - **Application transaction (shadow-git file sync, phase 4; git mode in phases 1 to 3 relies on git's own merge plus an expected-ref update, with no journal):** serialize applications to the same canonical target workspace across runs with a separate native target lock (section 6). Before changing any user file, index or branch ref, durably record an operation id, target identity/ref, prepared commit/tree, expected per-path contents/types/modes, before-images, desired outputs, and verification/confirmation identity in a journal under `.agents/apply/`. Write a progress record for each mutation; update refs with expected-old-value checks. Multi-file delivery is not filesystem-atomic. After interruption, reconcile paths equal to before/after images and complete or roll back idempotently; a path matching neither is a concurrent user edit and blocks recovery without overwriting it. Do not admit another application to that target until recovery is resolved. Revalidate ancestor directories and reject traversal through symlinks/junctions or paths outside the target; unsupported file types fail before writes. In git mode, preparation and confirmation are bound to the prepared plan (target ref, observed tip, prepared commit, changed paths): the plan is prepared again immediately before installation, any difference invalidates the confirmation (exit 7), and the expected-ref compare-and-swap catches the remaining race. Phase 1 has no operation journal and no durable operation id; `op_id` only correlates the `apply.*` events of one invocation (section 11).
  - Outcome is recorded as `apply.finished` (section 11). In git mode that is `{op_id, status, error?}` with status `applied`, `refused` or `failed`, and reapplying the same run to another target is a distinct invocation. In the phase-4 shadow-git transaction it is keyed by operation id, target identity and delivered tree, not by run id alone. A crash after file installation but before that event is recovered from the journal. Advance only the workspace delivery ledger after successful sync, never the run's baseline. Application failure has its own status and does not rewrite a previously successful run as failed. Original final-integration merge/verification failure still fails the run.
- **Merge ancestry:** both automatic and resolved merges publish a merge commit with the contributing output commits as parents (or fast-forward when possible). A resolver's ordinary single-parent attempt commit supplies the verified resolution tree only; the supervisor publishes that tree with the original merge parents. Never flatten a fan-in into a single-parent commit, which would lose ancestry and reintroduce resolved conflicts in later fan-ins. Conflict-task scope includes the full combined delta of the merge inputs, with resolution edits restricted to declared unresolved paths unless explicitly widened. Keep the per-attempt output refs (`refs/relay/<run>/<node>@<attempt>`, above) and merge refs under the `refs/relay/<run>/...` namespace until explicit run purge (`relay clean --purge-runs`) so worktree pruning, retries, and Git garbage collection do not remove reusable snapshots.

### 8.4 Sub-agents requesting work (phase 4)
A result may include `spawn_requests`. The router validates each against `max_depth`, `max_nodes`, remaining budget and permission policy, then adds accepted nodes to the DAG. Rejected requests are logged in `events.jsonl` and returned to the requester's summary. No unbounded recursion.

Requests may add fresh nodes and edges into those nodes only; they cannot change inputs of running or completed nodes. Added nodes undergo the full graph validation (section 8.1: acyclicity and reserved-path checks) and write-scope reservation checks before admission. Conflict resolution is an internal operation with an explicit supported task kind (`resolve-conflict`), not an unknown user taxonomy category, and cannot recursively request another resolver for the same merge.

### 8.5 Cost admission and accounting (phase 4)

- `policy.cost_mode` is `advisory` by default; `cost_ceiling_usd` and node `max_cost_usd` are scheduling thresholds in this mode, not guaranteed billing caps. Node limits cover all attempts, validation-repair calls, escalation, and fallback. Run accounting also includes classifier/planner calls, conflict resolution, and spawned work.
- Pricing basis and settlement: rates (`input_per_m`, `output_per_m`, `cache_read_per_m`, `cache_write_per_m`) are snapshotted with currency, source, and effective date. Adapters normalize provider usage into disjoint uncached-input, cached-read, cache-write, and output token counts. If a provider includes cache reads in total input, subtract them before applying the uncached rate; never charge the same tokens twice. Compute USD as the sum of each disjoint bucket times its per-million rate, divided by 1,000,000: `(uncached_in * input_per_m + cache_read * cache_read_per_m + cache_write * cache_write_per_m + out * output_per_m) / 1,000,000`. Missing rates or ambiguous usage are labeled `estimated` or `unknown`, never exact or zero; a cache rate omitted from `agents.yaml` counts as missing for labeling purposes even though section 3 supplies the `input_per_m` placeholder for the arithmetic, so such costs are `estimated` and never satisfy hard mode. Direct monetary cost takes precedence, with its billing basis recorded (metered API charge versus subscription-equivalent estimate). Unknown pricing cannot qualify for hard mode.
- Before each paid invocation, the scheduler atomically records a reservation under the run's ownership lock (the native lock from phase 2b; where 2b is skipped, the foreground interim lock of section 6). Available budget is the ceiling minus settled charges and outstanding reservations. Advisory reservations use a documented estimate; if it does not fit either the run or node remainder, select a cheaper eligible model or mark the node `blocked` with reason `budget_exhausted`. No retry or fallback bypasses admission.
- `cost_mode: hard` is accepted only when every paid invocation has an enforceable upper charge bound from adapter-supported limits and a known pricing basis. Reserve that upper bound. Final-only usage reports, estimates, timeouts, and process cancellation alone cannot establish such a bound. Reject the configuration before execution if any selected invocation cannot satisfy it; runtime fallback must satisfy the same condition. Among the verified CLIs only Claude exposes a spend flag (`--max-budget-usd`, print mode only); whether it is a strict upper bound or is checked between turns is unverified, so until a canary proves a strict bound no adapter qualifies and `hard` is rejected everywhere.
- On completion, settle the reservation using reported usage and its pricing basis, or a labeled estimate in advisory mode; refund the unused reservation. Unknown charges retain their reservation until reconciled. Persist settlement idempotently by attempt id so resume neither loses nor double-counts charges. Interrupted workers must be reconciled or terminated before a replacement attempt is admitted.
- If advisory charges exceed a reservation or reach the ceiling, record the overrun and stop admitting paid work. Workers already running may incur further charges; request cancellation and retain their outstanding reservations until settlement. Status reports show settled cost, reserved cost, estimate/unknown labels, and enforcement mode.

### 8.6 Supervisor lifetime and background execution (phase 2b)

Phases 1 and 2a have no supervisor: execution stays in the foreground, `wait` semantics do not apply, and `status`/`result`/`routes`/`explain` read the run directory directly. Phase 2c also runs in the foreground through section 8.7; its session service does not require the machinery below. Foreground ownership is defined in section 6. The remainder of this section describes detached operation in phase 2b and front-ends that use it.

Both front-ends start or attach to a per-run supervisor process implemented by the shared core. It owns scheduling, workers, verification, locks, and durable state; it does not live inside an OpenCode tool invocation. Start it detached with a hidden window on Windows and redirected logs, then wait for an IPC-ready acknowledgement before returning a run id. Persist the supervisor identity and an authenticated local endpoint in the run directory (`ipc.sock` Unix domain socket with 0600 permissions on Unix/macOS; named pipe `\\.\pipe\relay-<run-id>` with an owner-only DACL on Windows, address recorded in `ipc.endpoint`). If the canonical `.agents/runs/<run-id>/ipc.sock` path on Unix exceeds 100 bytes (approaching OS `sun_path` limits of 104–108 bytes), bind the socket under the platform temp directory (`$TMPDIR/relay-<repo-hash>-<run-id>.sock`, permissions 0600) and record the actual path in `ipc.endpoint`. CLI/plugin status, cancellation, replies, and resume use this endpoint; no agent task needs to remain attached to keep execution alive.

**IPC threat model.** Workers run as the same OS user as the supervisor, and at `full` permission they can read any file that user can read, including run-directory tokens and the IPC endpoint itself; file permissions (0600, and especially on Windows) therefore cannot keep a compromised or prompt-injected agent out. The design does not rely on them for protection of privileged operations:
- Commands are split into **observer** (`status`, `result`, `routes`, `explain`, `replay`; authenticated by `ipc.token`) and **privileged** (`apply`, `reply`, `cancel`, `resume` (confirmation is required only for `--raise-budget`, section 14), `clean --purge-runs`, `spawn` approvals, permission changes).
- Use available OS peer identity APIs and reject known worker-tree clients for privileged commands. Peer capabilities differ by platform; some APIs expose credentials without a usable process ID. Treat this as best-effort misuse detection, never proof of user intent or a security boundary against same-user unrestricted workers.
- Privileged commands require operation-scoped confirmation in an interactive CLI, explicit caller `--yes`, or an available host permission flow. Bind confirmation to a prepared operation digest (run/target, action, content, limits, and expected state revision); do not let a model-provided boolean count as host approval. Where no host permission API is available, return an approval-required result and the concrete CLI command. The supervisor cannot prove that `--yes` was typed by a human: an unrestricted same-user process can invoke it too. Confirmation is an operator workflow guard, not isolation from `full` workers. Stronger protection requires a separately trusted broker/identity or an enforced sandbox denying worker access to control state and IPC; v1 does not claim such a boundary at `full`.
- Worker environments never contain the token, endpoint address or run-directory secrets (section 9.4), and `prompt.md`/`stdout.log` are redacted for them.
- Residual risk, documented for users: an agent running at `full` can access user files, IPC, and `.agents/` state. Git recovery covers captured repository edits only and does not bound external damage. Only a verified sandbox profile establishes a restricted boundary; peer checks, confirmation prompts and token omission do not substitute for it.

`wait` controls only the caller's wait for a result. A wait deadline returns the actual persisted state (which may be pending, blocked, or running) without cancelling the run. Closing a CLI or reloading the plugin disconnects the observer. Explicit user session abort cancels runs owned by that session; expiration of a tool wait or client request does not. Persist the originating session/run association so aborts and reconnection can be reconciled after plugin reload. Supervisor death follows section 6 recovery; there is no automatic second owner on a missed heartbeat.

### 8.7 Foreground interactive coordination (phase 2c)

`SessionController` is an application service over the existing core, not another scheduler. It provides session creation/opening, turn submission, proposal inspection/approval, run start/resume, revisioned owner commands, snapshots, event subscription/replay and awaited shutdown. The TUI calls it in process. Existing CLI commands retain their entry points; shared execution and prepared-delivery services serve both. At most one graph is active per session and one session owns execution in a TUI instance. Historical sessions may be inspected without acquiring mutation authority.

The normal flow is operator prompt → bounded clarification/context gathering → validated plan preview → approval → routed parallel workers → verified handoffs → coordinator review → result or another proposal → explicit prepared apply. The coordinator is adaptive about decomposition and review; deterministic code enforces the section 5.6 approval envelope and section 7.4 capabilities. Every revised graph is approved before execution. Automatic retries are existing core behavior inside that envelope, not permission for the coordinator to extend its own work.

**Approval and admission.** Under session ownership, persist a unique run allocation and launch intent bound to the approval digest before creating or dispatching that round. The intent records the expected session/plan revision and is consumed idempotently. A retry or crash after run creation attaches to the recorded run; it cannot allocate a second run for the same intent. Before every admission, verify the durable gate, current revision, intent and approved policy. Recovery reconciles session intent, run metadata and events before doing anything paid. A superseded revision or unconfirmed old owner cannot execute. Reopening a session never automatically dispatches or repeats a coordinator call; the UI first shows the recovered state and offers an explicit continuation.

**Active questions and commands.** The owner mailbox accepts typed commands with command IDs and expected revisions; duplicate IDs with identical content are no-ops and conflicting content is rejected. A node answer includes its question revision and submission provenance, and is recorded through the active run's shared writer. Eligibility depends on that node's durable question set, even while independent nodes run. Requeue the answered node only within its existing limits, and reopen descendants blocked solely by that dependency; other failures/cancellations remain blockers. Answers change the input key and use the section 8.2 fresh-session rules. The scheduler does not declare the invocation complete while admitted owner commands are unreconciled. External `relay reply` retains its quiescent-only ownership guard; live TUI answers do not bypass it by writing files from another owner.

**Checkpoint and revise.** A steering submission first durably closes the admission gate, before processing queued answers, retries or new dispatch. Already admitted work may finish, or the operator may explicitly cancel it; cancellation waits for the managed process and actual worker thread to stop. Record answers while the gate is closed if their revisions still match, but do not dispatch them. Do not replace a prompt underneath an active worker. After active work settles, seal a checkpoint and mark the old round superseded in session metadata; an incomplete underlying run stays `interrupted` with a checkpoint reason, never `done`. Its remaining graph cannot resume through either front-end while superseded. If the operator abandons the revision, explicitly restoring that same frozen round requires revalidation and a durable gate transition; it is not implicit in reopening the UI.

The checkpoint includes only successful, validated output commits and their dependency closure, with immutable result/artifact hashes and verification evidence. Combine compatible contributions through the existing snapshot/merge primitives, recording ancestry; canceled or failed worker edits remain diagnostic worktrees and never become a base automatically. If combination conflicts, keep the separate source commits and conflict manifest visible. A new approved, bounded repair round may name those exact source commits and conflict paths; it cannot claim a merged base exists or receive unbounded write scope. No checkpoint is deliverable until the required integration checks in section 10 have passed (or an explicit operator skip is recorded as such).

**New rounds and unapplied follow-ups.** Every revised approved graph becomes a new linked run. Ordinary answers, retries and interruption recovery use the old frozen graph. Core graph intake gains an explicit validated retained-base input for session rounds, separate from the existing headless default of pinning repository HEAD. Pin the full commit/tree, source lineage and retained refs; never resolve a moving branch at dispatch. Approved checkpoint/import manifests are validated for source identity, hashes, ancestry and availability before work starts. A successful unapplied integration can be the next round's base, so iteration does not require applying changes to the primary checkout. Final delivery uses the cumulative session baseline, scopes and checks, not only the last round's delta (section 10).

**Foreground lifetime.** The service owns coordinator calls, workers, verifier processes, capture/merge threads and state writers independently of Textual widgets. Closing a screen does not cancel execution. Closing the application or explicitly stopping execution closes admission, requests managed cancellation, drains real process/thread completion, persists an interrupted session/run as appropriate, and only then closes writers and releases locks. Repeated cancellation, a second real SIGINT and cancellation of all asyncio tasks must not release ownership while a thread can still publish. If the owner dies, reopening uses section 6's identity/termination proof before recovery; unknown old execution stays blocked. There is no implicit detach or background survival in phase 2c. Session UI phases are projections (section 14.1), not extra `RunState` values. Detached lifetime and IPC can later implement the same application-service interface under section 8.6.

---

## 9. Permissions

**Default: highest permission** for worker CLIs, so tasks do not stall on prompts. Worker restrictions are explicit and cumulative. Coordinator invocations are a separate restricted capability (section 7.4); the worker default never authorizes unrestricted planning.

### 9.1 Levels

| Level | Meaning |
|---|---|
| `full` (default) | Agent may edit files, run any shell command, use network and tools without approval. |
| `workspace` | Repository writes are confined to the node worktree. Explicit writable exceptions for node output, scratch space, and required CLI state are recorded in the enforcement profile. Other filesystem writes are denied, including from shell subprocesses. Read and network allowances are declared separately. |
| `readonly` | Agent may read and analyze the input snapshot; repository writes and mutating tools are denied. Only declared node-output, scratch, and required CLI-state paths may be writable. |

### 9.2 Where to set it, and precedence
- CLI: `relay run --permissions readonly`, `--agent-permissions codex=workspace,claude=full`.
- `agents.yaml`: `defaults.permissions` and per-agent `permissions`.
- Task spec: `permissions:` per node.
- OpenCode plugin: tool argument `permissions` on `run_agent_task` / `run_task_graph`, and plugin option `permissions` in `opencode.json`.

All explicit restrictions are cumulative: the effective level is the most restrictive of task, CLI/tool argument, applicable agent config, plugin option, and global default (`readonly` < `workspace` < `full`). An omitted value adds no restriction. Source order never widens an explicit restriction. An unknown level value (anything but the three named levels) is rejected outright, so a misspelling cannot silently fall back to a default or a wider level. A node marked as read-only by the planner also imposes `readonly`, even when the run default is `full`.

### 9.3 Mapping and enforcement
Adapters implement the mapping in the table in section 7.2. Restricted levels require a concrete enforcement profile identifying the OS, CLI version, sandbox mechanism, writable exceptions, tool restrictions, and read/network policy. Enforcement profiles rely either on CLI-native sandboxing (e.g. Codex `-s workspace-write` backed by bubblewrap on Linux or seatbelt on macOS) or host OS sandbox wrappers managed by `relay` (e.g. `bwrap` on Linux, `sandbox-exec` on macOS; on Windows only filesystem-confining mechanisms qualify, such as AppContainer or a restricted token with a write-restricting DACL, or running the agent in WSL/a container; plain Job Objects limit CPU, memory and process trees but do not restrict file access and do not qualify). Until such a profile is implemented and passes the `relay doctor` canaries, native Windows `workspace` is reported unsupported for every agent that has no verified CLI-native sandbox on Windows, and `readonly` is advertised only when the complete profile prevents repository mutation by all enabled tools and subprocesses. An after-the-fact immutability check detects violations but cannot enforce the level. Early Windows releases may therefore expose only `full` for an adapter; read-access nodes requiring `readonly` fail closed until a qualifying profile exists. Approval flags or a prompt alone do not establish workspace confinement. As a result, Antigravity advertises `full` only, and Claude and Codex advertise `readonly` (never `workspace`) only on platforms where the `readonly` canary returned `enforced` (spike 0.12, Windows 11: Claude Code 2.1.310 `--permission-mode plan` and Codex 0.164.0 `-s read-only`; recorded in `READONLY_VERIFIED_PLATFORMS`). Linux and macOS advertise `full` only until their canaries are run. The canary writes a marker directly; its subprocess and symlink variants (below) are not yet exercised, so this is evidence for the direct-write case only. Because read-access nodes (section 8.1) require `readonly`, those nodes have no eligible agent on a platform until a qualifying `readonly` profile is promoted there; qualifying it is release-blocking P0 work for phase 2a sign-off (development-plan tasks 0.12 and 2.30), not optional hardening. No setting lets a read node run under `full`. A passing canary does not by itself promote a profile into `probe().levels`: promotion is a manual adapter change made only after the canary returns `enforced` for the complete profile on the target platform. On platforms where neither CLI-native nor host-managed sandbox mechanisms exist for a given agent, `workspace` is excluded from `probe().levels`, and tasks requesting `workspace` fail closed with an explicit error rather than running unconfined.

`relay doctor` checks each profile in disposable directories, including an allowed write, denied writes outside the worktree (also through a shell subprocess and a symlink), denial of repository writes for `readonly`, and access to declared node-output paths. Each canary classifies the outcome as `enforced`, `bypassed`, `inconclusive`, `unavailable` or `error` from filesystem evidence alone — never from the CLI's exit code, which proves nothing on its own. A `readonly` canary treats CLI-state writes next to the repository fixture (caches, session files) as non-mutations: only writes inside the repository fixture count as a bypass. The canary result reports the mapped flags and whether the level is advertised, and never changes `probe().levels`. Unsupported OS/version combinations are excluded from `probe().levels`; flag snapshots alone are insufficient. If a requested level cannot be honored, the router picks an eligible agent or fails a pinned node with a clear error. It never silently widens permission. Worker enforcement covers the agent process tree; the orchestrator's candidate capture and verification use a separately recorded profile, and read-only nodes do not run build/test commands that mutate their checkout.

### 9.4 Safety nets that remain on at `full`
- Worktree and branch per write node preserve captured repository edits for inspection and reversal; they do not undo external effects of unrestricted shell commands. Pre-run `git status` must be clean unless `--allow-dirty` is supplied. `--allow-dirty` snapshot semantics: section 8.3.
- Secrets: the child environment is built from a platform-safe base allowlist plus variables each CLI needs for auth (e.g. `CURSOR_API_KEY`, `GEMINI_API_KEY`, provider keys); the parent environment is not forwarded wholesale. The base allowlist includes platform-essential variables: on Windows, `SystemRoot`, `SystemDrive`, `WinDir`, `ComSpec`, `PATH`, `PATHEXT`, `TEMP`, `TMP`, `APPDATA`, `LOCALAPPDATA`, `ProgramData`, `ProgramFiles`, `ProgramFiles(x86)`, `USERPROFILE`, `USERNAME`, `HOMEDRIVE`, `HOMEPATH`, `SSH_AUTH_SOCK`, `GIT_SSH`, `GIT_SSH_COMMAND` (omitting these causes Windows socket/runtime APIs, node spawns, `.cmd` shims, OpenSSH agent pipes, npm and git to fail); on Unix/macOS, `PATH`, `HOME`, `USER`, `SHELL`, `TMPDIR`, `LANG`, `LC_ALL`, `TERM`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `SSH_AUTH_SOCK`, `GIT_SSH_COMMAND`. On all platforms the base also passes common toolchain roots when present in the parent environment (`JAVA_HOME`, `GOROOT`, `GOPATH`, `CARGO_HOME`, `RUSTUP_HOME`, `DOTNET_ROOT`), network and trust settings so CLIs work behind corporate proxies and TLS inspection: `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY` (either case), `SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and each CLI's config-home override when set (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME` **[verify name]**; Antigravity has none documented — its settings live at `~/.gemini/antigravity-cli/settings.json` **[verify]**). Proxy URLs may contain credentials, so they are passed but never logged. On Windows, allowlist lookup and override merging are case-insensitive, and known variables use the canonical spellings listed above; this preserves `SystemRoot` when the parent mapping supplies `SYSTEMROOT` and prevents duplicate `PATH`/`Path` entries. POSIX name matching remains case-sensitive except for proxy variables. Extra variables can be injected via `env:` in `agents.yaml`; the supervisor's own variables (IPC token, endpoint, run secrets) are never forwarded.
- **Prompt-injection hygiene**: content originating from other agents or from repository files is wrapped and labeled as untrusted data in downstream prompts. The router executes only verification commands resolved from user-authorized project configuration or graph/task specs (`commands`, executable `acceptance`, and `integration_acceptance`). Spawn requests cannot introduce or alter those commands or disable verification; they inherit the authorized verification policy.
- A banner at run start states active permission levels per agent; `events.jsonl` records the level actually used by each node.

Phase-2c context reads are Relay-owned, bounded operations over the approved repository's pinned Git objects or explicitly approved, hash-validated artifact ranges. They are not arbitrary filesystem reads or model-supplied shell commands. Reject absolute paths, traversal, reserved runtime paths, authentication/config-home paths and selections outside the approved source; do not follow repository symlinks, junctions, submodule paths or artifact links into host files. A tracked symlink may be described as link data without dereferencing it. Enforce size limits before materializing large objects. Approved artifacts are a narrow exception to reading runtime storage: resolve the exact manifest entry with containment and link checks, never expose its parent directory. Operator-provided context and agent/repository text retain their provenance; quoting a command in such text cannot authorize executing it. Public progress and inspectors follow section 11's redaction and terminal rendering rules.

---

## 10. Verification

The Verifier runs after every write node and after merges, against the captured candidate tree from section 8.3. It does not trust agent self-reports. Graph-level `commands` and `verify` are inherited by nodes unless explicitly overridden; repository defaults are supplied by the run's resolved project configuration.

1. `verify` defaults to `default` when omitted. In this mode, always run explicit executable `acceptance` entries (a `RunCheck`); if there are none, run the configured `build`, then `test`, then `lint` command, skipping unset entries. A `diff_scope` entry is a scope check and never counts as an executable check or suppresses defaults. If no executable check can be resolved for a write node, reject the task before dispatch with a `NoExecutableChecksError` rather than report an untested change as verified.
2. User-authored `verify: none` with a nonempty `verification_skip_reason` may explicitly skip command checks; the scope check is still enforced, and a scope violation still fails verification. Command checks are skipped and the outcome is recorded as `skipped` with that reason, never `passed`. A `skipped` outcome does not block publication (only `failed` does), but the run result records the distinct `skipped` outcome and its reason rather than reporting the change as verified. Agents and spawn requests cannot add this opt-out. Read-only analysis nodes record command verification as `not_applicable`; they still undergo result validation and repository immutability checks.
3. Scope checks use the complete captured delta, including staged, committed, and untracked deliverables. `owns` is a planning/scheduling constraint used to detect concurrency hazards and serialize parallel writers; `diff_scope` is the Verifier acceptance check constraint. The effective scope is the union of the `diff_scope` lists from every `ScopeCheck` acceptance entry; if there are none it defaults to `owns` (if specified), otherwise the entire repository. Every changed path — rename and copy sources and destinations, deletions, and binary files included — must fall within the effective scope, and a literal (glob-free) directory prefix or a glob pattern also covers paths beneath it. If both `diff_scope` and `owns` are explicitly provided on a task spec, modified files must satisfy `diff_scope` (allowing `owns` to specify parallel ownership while `diff_scope` explicitly permits generated tests or secondary files).
4. At merges and final integration, run `integration_acceptance` plus applicable scope checks. The aggregate diff is checked against graph-level scope and the union of allowed paths from contributing writers; individual writer scopes remain checked against their own input commits. If no executable integration acceptance is provided, use graph/repository build/test/lint defaults. Missing executable checks require the same explicit opt-out as write nodes. Integration checks run after all terminal outputs have been combined, even for a single-node write run. A graph containing only read-only analysis nodes with unchanged output trees records integration command verification as `not_applicable`.
5. Record results into `result.spec.yaml.verification`; failed verification publishes no successful output commit and triggers retry with model escalation and budget admission, feeding failing output back via a follow-up prompt, up to `max_attempts`. A node reaches `done` only after result validation, verification (or recorded explicit skip), and output publication succeed.
6. Optional `review` fan-in: a read-only review node (different agent than the author when possible) receives the dependency snapshot and diff from the pinned run base before the graph is marked complete.
7. Commands are split into an argument list by platform-correct rules — POSIX `shlex.split`, Windows `CommandLineToArgvW` — and run with no shell, using the candidate worktree as the working directory. Each command may carry its own `timeout_s`; the run loop additionally supplies an optional overall verification timeout. An optional `expect_output` value must appear as a substring of the command's stdout. Stdout, stderr and the exit code are captured and recorded in the verification details.
8. Verification runs in the candidate and must not edit the published tree. After the commands, the candidate HEAD must not have moved and no tracked file may have changed (`git status --porcelain`), ignoring untracked artifacts created by the commands; any such mutation fails verification rather than being silently accepted.

**Session delivery (phase 2c).** The shared CLI/TUI delivery service prepares the exact target-specific tree under section 8.3, verifies it, obtains confirmation bound to that prepared operation and expected target, and only then installs it. For a session-linked result, load the session's original delivery baseline and cumulative approved scope/check policy (section 5.6), validate all carried contributions, and check the entire cumulative final tree/delta. The most recent round's narrower scope or checks cannot replace earlier user-mandated requirements. Apply through a run ID must resolve its session policy too; it cannot bypass these checks. A failed/incomplete checkpoint is inspectable input for a repair proposal, not a successful integration. Changed target refs, policy, checkpoint ancestry or prepared content invalidate confirmation. The primary checkout changes only through this explicit delivery operation; coordinator `finish` and successful rounds never auto-apply.

The delivery policy is folded from the *approved* graphs of the session's rounds up to and including the delivered one, and it only accumulates: the write scope is the union of the approved write scopes (unrestricted once any approved write node had none), every executable integration check ever mandated stays required, and each round's integration scope restriction must hold. A later round's narrower scope or `verify: none` adds a recorded skip and removes nothing. The session record's copy of the policy is evidence, not authority: it must equal the fold of the rounds it names, or delivery is blocked. Carried contributions are revalidated from the delivered round's base chain: each checkpoint must be sealed, deliverable (section 8.7), unchanged in result/artifact hashes and lineage and an ancestor of the delivered commit, and each unapplied integration must be a finished round of this session whose integration branch and node output refs are intact. Any failure blocks delivery and lists every problem; a checkpoint can also be delivered directly. The session delta from the original baseline is checked against the scope, the checks run on the prepared tree, and confirmation binds to a digest over the target ref and tip, prepared commit and paths, policy, carried identities and evidence, and the session revision.

---

## 11. Observability

- `events.jsonl` is the canonical, append-only record: one JSON object per line, with flat fields `seq` (starts at 1, no gaps), `ts` (UTC, millisecond precision), `type`, `owner_generation`, optional `hashes` (spec hashes), then the payload fields of the catalog entry. An append is one line flushed to disk before the call returns, so a decision logged before dispatch survives a crash. When the log is opened, a torn trailing line (no newline and not valid JSON) is discarded and a complete trailing line missing only its newline is repaired; an unparsable earlier line is a corruption error, never skipped. Events carry no answer text. Catalog:
  - **Run and supervisor:** `run.started {config_hash, base_commit, allow_dirty, sensitive_paths?}`, `run.finished {state, integration_commit?}`, `supervisor.started {generation}`, `supervisor.recovered {generation, reconciled_nodes}`, `supervisor.lock_failed`.
  - **Node lifecycle:** `node.state {node_id, from, to, reason?}` (every transition, including `blocked` with its reason such as `needs_input`, `budget_exhausted`, `dependency_failed`), `attempt.phase {node_id, attempt, phase}` (`agent|capture|verify|publish`, section 8.2), `node.started`, `node.finished`, `node.reused|invalidated`.
  - **Routing:** `node.routed`, `route.log.write_failed`.
  - **Input:** `input.requested {node_id, question_revision}`, `input.recorded {node_id, question_revision, submission_id, provenance}` (answer content lives in `input_answers.yaml`, only its hash is in the event), `input.rejected {reason}`.
  - **Verification, merge, delivery:** `verify.result`, `merge.result`, `integration.published`, `apply.started {op_id, target, commit}`, `apply.progress {op_id, step}`, `apply.finished {op_id, status, error?}`, `apply.recovered {op_id, action}`. In git mode the `apply.finished` status is `applied`, `refused` or `failed`, and progress steps include `prepared`, `verification-passed`, `verification-skipped`, `verification-failed`, `verification-not-recorded`, `verification-not-configured`, `confirmed`, `ref-updated` and `installed` (section 8.3).
  - **Budget, spawn, other:** `budget.*`, `spawn.*`, `permission.denied {node_id, requested, effective}`, `warning`.
  - Detailed fields for the main events:
  `run.started`, `node.routed {node_id, route_seq, decision}` (canonical routing entry; route logs are projections, section 4.4), `route.log.write_failed`, `node.started {permissions, enforcement_profile, input_commit, input_key, cmd}`, `node.finished {status, output_commit, exit, duration, usage}`, `node.reused|invalidated`, `verify.result`, `merge.result`, `integration.published`, `apply.started {op_id, target, commit}`, `apply.finished {op_id, status, error?}`, `budget.reserved|settled|exhausted|overrun`, `spawn.accepted|rejected`, `warning`, with timestamps and spec hashes.
- `relay status [RUN] [--json]` renders a run record and its event history, or lists runs, from `run.json` and `events.jsonl` (section 14); the OpenCode `task_status` tool renders a node table.
- `relay replay <run>` re-renders the timeline; `relay explain <run> <node>` and `relay routes` are defined in section 14.
- Cost/usage is taken from CLI JSON output where available (Claude, Antigravity, Gemini stats), otherwise estimated and flagged as estimated or unknown. From phase 4, status exposes settled and reserved costs and `advisory|hard` mode; advisory ceilings are never presented as guaranteed billing caps (section 8.5).

**Interactive projections (phase 2c).** Each session has its own sequenced durable event stream, using the same append/replay discipline; run streams remain canonical for execution. Session events include `session.created`, `turn.started|finished`, `coordinator.started|finished`, `context.selected`, `plan.proposed|approved|superseded`, `launch.prepared|linked`, `admission.paused|resumed`, `checkpoint.started|sealed|conflicted`, `delivery.policy`, and `session.interrupted`. Events carry IDs, revisions, hashes, states and evidence references, not prompt/answer text or raw environments. Coordinator question revisions are session-scoped; worker questions keep their run/node revisions. There is no fabricated total sequence across streams: projections join by session/run/command IDs and retain a cursor for each stream. Rebuild from a consistent snapshot and subsequent durable events; repeated notifications and reconnect replay must be idempotent.

Notify subscribers only after the durable append. Public progress is a separate, optional, lossy channel identified by call/run/node/attempt; it never changes state or authorizes actions. Bound each subscriber queue (default 256 records, at most 8192 UTF-8 bytes per record); split oversized text frames and coalesce/drop transient frames under pressure with an explicit gap marker. A slow or disconnected UI cannot block process-pipe draining, cancellation or owner commands. Final output capture/parsing remains independent of the public queue; use bounded spooling and fail explicitly on its configured resource limit rather than silently truncating a structured result into apparent success. Unsupported progress capabilities show phase events and completed output only.

Redact known secrets and control-state credentials before publishing progress or rendering retained logs. Treat content as plain untrusted text: strip terminal escape/control sequences, disable interpreted markup, and allow source navigation only through validated references. Logs are available in an inspector subject to retention limits, with omissions/truncation labeled; they are not appended to handoffs or coordinator memory automatically. The UI shows distinct claimed results, verifier evidence, skipped checks and unknown usage instead of equating fluent output with verified success.

---

## 12. Alternatives considered

| Option | Why not (for v1) |
|---|---|
| MCP server exposing each CLI to one lead agent | Delegation becomes non-deterministic, no DAG scheduling, hard to enforce permissions and verification. Kept as a possible additional front-end later (core is reusable). |
| A2A / Zed Agent Client Protocol transports | Local CLIs already have headless modes; protocol layers add setup without benefit now. The adapter interface is protocol-neutral so an ACP/A2A adapter can be added. (Note: Zed's ACP, editor↔agent, is distinct from IBM's Agent Communication Protocol.) |
| LangGraph / agent frameworks | Heavy dependency for what is subprocess supervision plus files. |
| Single shared transcript between agents | Large, agent-specific, leaks injection; specs with summaries and references are smaller and auditable. |
| TypeScript core (Node/Bun) shared with the plugin | Needs a native lock binding per OS/architecture/runtime, a Node or Bun runtime matrix, and tree-kill workarounds; the plugin only needs a thin client, so sharing code buys little. |
| Rust or Go core (single binary) | Better distribution and compile-time state machines, but slower to build; the workload is subprocess-bound, so speed is not a factor. Revisit if a zero-dependency install becomes a requirement (a PyInstaller build is the first step). |

---

## 13. OpenCode integration (phase 3; surface 2 phase 5)

Findings from the OpenCode docs: plugins are TS/JS modules (`.opencode/plugins/`, `~/.config/opencode/plugins/`, or npm via the `plugin` array in `opencode.json`) exporting `async ({ project, client, $, directory, worktree }) => hooks`. Hooks include `tool` (custom tools with `tool()` and zod from `@opencode-ai/plugin`; tool context has `agent, sessionID, messageID, directory, worktree`), `tool.execute.before/after`, `permission.asked/replied`, `session.*` events, `shell.env`. Agents are defined in `opencode.json` or `.opencode/agents/*.md` with `mode: primary|subagent|all`, `model`, `permission` (including `permission.task`), `prompt`, `steps`. The SDK offers `client.session.create/prompt/children/abort`, event subscription and structured output (`format: json_schema` → `info.structured_output`). Custom model providers are configured through `provider.<id>` with `@ai-sdk/openai-compatible` and `options.baseURL`; the docs do not show plugins registering providers.

### 13.1 Surface 1: router tools (primary, phase 3)

The plugin registers tools; OpenCode's LLM is the planner, the plugin is the deterministic scheduler/adapter layer:

| Tool | Args (zod) | Returns |
|---|---|---|
| `route_task` | `goal`, optional `title`, `category`, `files`, `complexity`, `agent`, `permissions` | routing decision `{agent, model, effort, agent_rule, model_rule, reason}` without executing; writes a `previewed` route log entry |
| `run_agent_task` | a task spec (or `goal` + hints), `wait: boolean` (default: `false`), `wait_timeout_s` (default: 45) | with `wait: true`, result summary when complete, otherwise `{run_id, node, state: <actual persisted state>}` on wait expiry; with `wait: false`, `{run_id, node}` after supervisor-ready acknowledgement |
| `run_task_graph` | graph spec, `policy`, `wait: boolean` (default: `false`), `wait_timeout_s` (default: 45) | same wait semantics; run id and node table, final summaries when complete |
| `route_log` | `run_id`, optional `tail`, `node`, `agent`, `rule` | route log entries (section 4.4) |
| `task_status` | `run_id`, optional `node` | state table |
| `task_result` | `run_id`, `node`, `field?` | result spec (or a field) |
| `task_input` | `run_id`, `node`, `question_revision`, `submission_id`, `answers` (string or map) | records current answers after host approval; returns actual node state and any exhausted-limit reason |
| `task_apply` | `run_id`, optional `branch`, `force` | privileged (sections 8.3, 8.6); returns `{run_id, commit, status, conflicts?}` or `approval_required` |
| `task_cancel` | `run_id`, optional `node` | cancellation outcome |

Companion assets installed by the plugin (copied to `.opencode/agents/` and commands on first run, never overwriting user edits):
- `router` primary agent: prompt explains the spec contract, categories, when to call `route_task` vs `run_task_graph`, and to prefer fan-out for independent work.
- `cli-worker` subagent (read-only helper that can call `task_result` to digest outputs).
- Commands: `/route <goal>`, `/fanout <graph file>`, `/runs`.

Behavior notes:
- OpenCode `permission` settings govern who may call the router tools; `permission.task` governs which OpenCode subagents the `router` agent may start. The plugin's `permissions` argument governs the external CLIs (section 9).
- External workers are mirrored into OpenCode: progress is emitted with `client.app.log` and, optionally, as child sessions created via `client.session.create` that hold a short transcript of the worker's summary, so the TUI shows what is running. Mirroring is best-effort; the run store is the source of truth.
- Waiting and cancellation semantics are defined in section 8.6. Plugin specifics: both execution tools default to `wait: false` and return the run id after supervisor-ready acknowledgement; `wait: true` blocks at most `wait_timeout_s` (default 45) and returns the actual persisted state on expiry. Explicit user session abort maps to `task_cancel` for runs owned by that session; an undifferentiated request abort signal alone is not treated as user cancellation. Poll `task_status` for authoritative completion; session-idle events are not proof, and completion notifications are best-effort projections of supervisor events.

### 13.2 Surface 2: provider gateway (optional, phase 5)

To select an external CLI as a *model* inside OpenCode (`/models`), the plugin starts a local OpenAI-compatible server (`/v1/chat/completions`, `/v1/models`) on a loopback port with a random bearer token. Model ids: `router/auto` (route by category), `router/claude-deep`, `router/codex`, etc. Each request becomes a single-node task run, and the final text is returned (streamed as SSE chunks of the CLI's progress/summary).

Configuration: a `provider.router` entry (`npm: @ai-sdk/openai-compatible`, `options.baseURL`, `options.apiKey`, `models`) that the plugin either injects (if the plugin API exposes a `config` hook; **[verify]**, present in plugin source types but not in the docs) or prints as a snippet via `relay opencode-config`.

Limitations, documented for users: the CLI runs its own tool loop, so OpenCode tool-calls are not passed through; no token-level streaming fidelity; context from OpenCode's conversation is flattened into the prompt. Surface 1 is the recommended path for programming workflows; the gateway is for "use that CLI as my model in this session".

### 13.3 Packaging and install
- npm package `@agent-relay/opencode-plugin` listed in `opencode.json` `plugin`, or a local file in `.opencode/plugins/`. It requires the `relay` executable (the Python package, installed with `uv tool install` or `pipx`) and checks its version at load.
- The plugin is the only Node code in the project; the core spawns CLIs through Python's `subprocess`/`asyncio`.
- Version compatibility: plugin declares the `@opencode-ai/plugin` version range it was tested with; `relay doctor` checks it.

---

## 14. `relay` CLI

```
relay run ("<request>" | --task FILE | --retry RUN) [--agent A] [--category C] [--permissions full|workspace|readonly]
                     [--agent-permissions a=level,...] [--model-tier fast|balanced|deep]
                     [--check COMMAND]... [--build COMMAND] [--test COMMAND] [--lint COMMAND]
                     [--verify default|none] [--verification-skip-reason TEXT]
                     [--max-attempts N] [--timeout SECONDS]
                     [--concurrency N] [--dry-run] [--allow-dirty] [--exclude GLOB]... [--non-interactive] [--json]
relay plan "<request>" [--template bugfix|feature|refactor|upgrade] [--check COMMAND]... [--output FILE] [--json]
relay graph run graph.yaml [--concurrency N] [--allow-dirty] [--non-interactive] [--json]
relay status [RUN] [--json]                # one run, or all runs under .agents/runs
relay result <run> [NODE] [FIELD] [--json] # NODE defaults to a run's only node; FIELD is dotted
relay resume <run> [--raise-budget USD] [--max-attempts N] [--node ID] [--non-interactive] [--json]
                                  # resumes interrupted or blocked runs; --raise-budget unblocks nodes blocked with
                                  # reason budget_exhausted; --max-attempts explicitly raises the total attempt cap
relay cancel <run> [node] [--yes] [--json]
relay reply <run> <node> ["<answer>" | --answers-file F] [--revision N]
                                  [--submission-id ID] [--resume] [--yes] [--json]
                                  # answer questions and unblock a needs_input node (defaults to current revision if omitted); --resume re-runs it.
                                  # Phase 1: file-based, foreground resume; phase 2b: routed through supervisor IPC (section 8.2)
relay apply <run> [--branch B] [--force] [--yes] [--json]
                                  # privileged; --force is shadow-git only (phase 4) and rejected in git mode (exit 2);
                                  # behaviour, flags and confirmation: sections 8.3 and 8.6
relay explain <run> <node> [--json]
                                  # latest decision, decision history and `from` chain, alternatives, reason (section 4.4)
relay routes [run] [--tail N] [--node ID] [--agent A] [--model M] [--event E] [--category C]
                      [--rule ID] [--order oldest|newest] [--json]
                                  # route log (section 4.4); without <run>, reads all runs under .agents/runs
relay agents [--json]             # list agents, availability, versions, enforceable levels
relay doctor [--paid] [--check ID]... [--json]
                                  # probes CLIs, canary runs (incl. session-id extraction), config validation, git exclude block,
                                  # Windows long-path/worktree-root check, pricing-rate warnings, opencode compat
relay clean [RUN] [--older-than <dur>] [--purge-runs] [--yes] [--dry-run] [--json]
                                  # prune inactive worktrees/scratch; purge explicitly removes run records and refs
relay opencode-config              # print provider/plugin config snippet
```

The command block describes the complete phase-scoped interface; section 16 assigns each command to its phase. A later-phase command is rejected with its phase number. A command in the active phase without a handler is rejected as not implemented in that build.

**CLI response and error handling.** With `--json`, command responses are single JSON values on stdout. Command intake errors without a run-result payload, argument-parser errors, unknown-check errors and phase-availability errors are JSON objects containing `error` on stderr and leave stdout empty. Run-result payloads retain stdout for all exits, including pre-dispatch rejections, nonzero execution outcomes and reasons. Human diagnostics such as permission banners and reply instructions use stderr. Unexpected handler failures are mapped to exit 1 with an actionable message and no traceback; keyboard cancellation uses exit 4. Help and version retain their normal text output. Later-phase command errors take precedence over unknown flags on those commands. Argument errors retain exit 2.

**Single-node CLI intake.** `relay run` accepts exactly one request source: a positional request string, `--task FILE`, or the recorded run selected by `--retry RUN` (section 6). A relative task path is resolved against the repository selected by global `-C`. The file is a YAML or JSON task spec validated by section 5 before probing agents or creating a run. File `run_id` is replaced by the allocated run id; `id` is retained. The repository is selected by `-C`; file `repo.root` must be `.`, and supplied base-branch/input-commit values are rejected because this intake pins the checkout itself. Reserved node ref names are rejected before dispatch, and the output channel must retain its default result and artifact locations. Single-node intake rejects nonempty `depends_on` and `context.from`; use `relay graph run FILE` for a task graph (phase 2a). `budget.max_duration_s` caps cumulative active attempt duration across replies and resumes, excluding operator wait time; monetary limits retain their phase-4 rejection.

For a text request, `--category` overrides classification. For either source, explicit `--agent` and `--category` override the corresponding input fields. Repeatable `--check COMMAND` replaces executable acceptance checks while retaining scope checks. `--build COMMAND`, `--test COMMAND` and `--lint COMMAND` override individual default commands. `--verify default|none` and `--verification-skip-reason TEXT` use section 10's validation; a missing reason never implicitly authorizes a skip. `--max-attempts N` overrides the node attempt limit and `--timeout SECONDS` overrides its positive per-attempt timeout; limits must be positive integers. A task timeout otherwise wins over `defaults.timeout_s`. CLI permission caps remain cumulative with file permissions under section 9.2.

The fully merged task is revalidated before dispatch. Missing executable checks still reject a default-mode write task; commands are never inferred from an agent reply. The task's verification policy is recorded as the single-node graph's integration policy so delivery can verify the exact prepared tree under section 8.3. Permission banners go to stderr with `--json`, leaving stdout as one result JSON object. Existing routing, permission, dirty-base and output flags remain available.

For the single-node CLI, `--dry-run` is not implemented; `--concurrency` is accepted on `graph run`. `--non-interactive` selects the file-based input flow of section 8.2; JSON mode also suppresses inline prompts. `relay resume` accepts an answered, blocked single-node input run, an interrupted single-node run, or a recorded foreground graph; interruption and failed-run retry follow section 6. Resume uses the recorded configuration, effective permission cap and pinned input commit. Read nodes still require the readonly enforcement profile of section 9.3, so a `full`-only adapter is ineligible for a read node.

**Foreground graph intake (phase 2a).** `relay graph run FILE` loads a YAML or JSON graph relative to global `-C`, validates section 8.1 before dispatch, and allocates a fresh run id. Node command defaults inherit graph commands per key; omitted node verification inherits graph verification. Effective inherited settings are persisted for resume. Write nodes and graph integration require authorized executable checks or the explicit section 10 opt-out. `--concurrency` is a positive global pool bound; per-agent limits still apply. A graph returning `needs_input` persists the question set and exits blocked after independent work finishes; use `relay reply` and `relay resume` for file-based answers. Raising `--max-attempts` on graph resume increases incomplete nodes' total limits without resetting counts. Completed nodes follow section 6 reuse rules; a changed upstream invalidates descendants even when its resulting code is identical. Adapter execution fingerprint support and unknown cases follow section 6.

`relay plan` uses the selected deterministic template (default `feature`), writes `graph.yaml` or `--output FILE` relative to `-C`, and refuses to overwrite an existing file. Repeatable `--check` supplies implementation/test and integration acceptance. Review routing, scopes and verification before execution; this headless template command never calls an agent. Adaptive interactive planning is separate (section 8.7). `relay cancel` prepares a run-wide or node-specific cancellation request and requires confirmation or `--yes`; in phase 2a the foreground coordinator consumes atomic request files under its run's `cancel-requests/` directory. For foreground graphs in phase 2a, Ctrl-C stops active workers and preserves an interrupted graph for resume; single-node runs use Ctrl-C as described in section 6. Detached run ownership and IPC remain phase 2b (section 8.6); phase 2c separately uses native session ownership (section 6).

**Foreground reply intake.** A positional answer is accepted for exactly one pending question. For several questions, `--answers-file` loads a YAML or JSON mapping from question IDs to strings or lists of strings; relative paths use `-C`. Unknown IDs, missing required values, invalid choice values and duplicate mapping keys are rejected before confirmation. `--revision` defaults to the current question revision; `--submission-id` supplies an idempotency key, otherwise a fresh one is generated. Reusing an ID with different content is invalid input. Confirmation prints the affected node and revision; without confirmation, exit 7 prints the command with `--yes` and changes no run state. The foreground lock covers answer validation and persistence, with question-set revalidation after confirmation. Protocol behavior and provenance are defined in section 8.2.

**`relay routes` and `relay explain`** read the route log's canonical `node.routed` events (section 4.4) instead of the derived projection.

- `relay routes [RUN]`: without `RUN`, aggregates entries across every run directory that has an `events.jsonl`. Filters combine conjunctively: `--node`, `--agent`, `--model`, `--event`, `--category`, and `--rule` (matches either `agent_rule` or `model_rule`). Entries are ordered oldest-first by `(ts, run_id, seq, attempt)` for determinism; `--tail N` keeps the newest `N` entries before that display order is applied (`N` is zero or greater, else invalid input), and `--order newest` reverses it. `--json` prints `{"count", "entries"}` with full `RouteEntry` objects (section 4.4). An empty result is not an error: it prints `no routing decisions` (or `{"count": 0, "entries": []}`).
- `relay explain RUN NODE`: shows the node's latest decision plus its full decision history and `from` chain, the alternatives with scores, and the reason; `--json` returns `run_id`/`node_id`, the current entry, all entries, the chain, alternatives, and derived signals (`queued`, `rejected`, `exhausted`, `model_unavailable`, `structured`). An unknown run or node is a clean invalid-input exit with an actionable message (`relay routes <run> --node <node>` for a missing node); a torn trailing event record is tolerated (section 11), while earlier corruption is a store failure. With `route_log.include_goal: false` these commands print only what the entry holds — `-` for a null title and the structured reason code — and never reconstruct goal text or free-text reasons.

**Inspection commands.** `relay status`, `relay result`, `relay agents` and `relay clean` read the run directory and merged configuration directly; they never read the derived `routes.jsonl` projection, take the run lock, or mutate a run they are inspecting (phase 1, section 8.6).

- `relay status [RUN] [--json]`: without `RUN`, lists runs newest-first by `created_at` and then run id; a directory with no `run.json` or an unreadable record appears as `state: "unreadable"` with a reason instead of failing the listing. With `RUN`, prints the record (state, creation time, final integration commit, last event) and a node table (node, state, attempts, output, blocked reason). JSON is `{"count", "runs": [...]}`; a run carries `run_id`, `state`, `created_at`, `age_s`, `owner_generation`, `config_hash`, `final_commit`, `nodes` (`node_id`, `state`, `attempts`, `blocked_reason`, `output_commit`, `dir`), `reasons`, `last_event_ts` and `last_event_age_s`. A missing run or a path-like id is invalid input (exit 2); an unreadable `run.json` or event log is a store failure (exit 1).
- `relay result RUN [NODE] [FIELD] [--json]`: reads the node's current `result.spec.yaml`. `NODE` may be omitted when the run records exactly one node, and `FIELD` prints one dotted field (for example `changes.files`) instead of the whole payload. A missing result file is not an error: the payload reports the record's state with `result_found: false`. JSON keys are `run_id`, `node_id`, `run_state`, `node_state`, `status`, `result_found`, `result_path`, `summary`, `agent`, `model`, `output_commit`, `integration_commit`, `integration_branch`, `changes`, `verification`, `open_questions` and `artifacts`; absent result fields are null or empty. A missing run, unknown node or unknown field is invalid input (exit 2); an unreadable result is a store failure (exit 1).
- `relay agents [--json]`: lists the merged configuration and probes each enabled agent with `<cmd> --version` (free; no model call, no `--help`). JSON is `{"count", "agents": [...]}`; an entry carries `name`, `enabled`, `cmd`, `available` (null when disabled, false when the command is missing or the probe fails), `version`, `levels`, `permissions`, `categories`, `models`, `concurrency`, `cost` and `note`. `env` and `pricing` are never rendered. An invalid configuration is invalid input (exit 2).
- `relay clean [RUN] [--older-than DURATION] [--purge-runs] [--yes] [--dry-run] [--json]`: removes only registered worktrees under the run's own worktree directory and that run's scratch directory. `--older-than` takes a number and `s`, `m`, `h`, `d` or `w`, and skips younger runs. `--purge-runs` additionally deletes the run's refs (`refs/relay/<run>/...`, `refs/heads/relay/<run>/...`) and the run directory; it is privileged, so without `--yes` a non-TTY session exits 7 and changes nothing, and a declined prompt does the same. `--dry-run` prints the plan and changes nothing. A live run, or one whose owner cannot be checked, is skipped; naming it explicitly is a failure (exit 1) outside `--dry-run`, and it is left intact. Lock objects are never created, replaced or unlinked. JSON is `{"dry_run", "runs": [...], "removed", "errors"}`; a run entry carries `run_id`, `state`, `skipped`, `worktrees` (`path`, `candidate`, `removed`, `error`), `scratch`, `run_directory`, `refs` and `error`, and `removed` counts `worktrees`, `scratch`, `run_directories` and `refs`.

**`relay doctor`** runs a registry of checks with stable ids (`python`, `git`, `config`, `agent.<name>.cli`, plus those added by later sections). Each check reports `pass`, `warn`, `fail`, `unsupported` (not available on this machine or platform, with the reason) or `skipped` (a prerequisite failed). Checks that call a model are marked `paid`: they run only with `--paid`, or when named with `--check <id>`, so the default run costs nothing. Adapter canaries follow this split: `version` and `auth` are free, while `structured`, `session`, `quoting` and `needs_input` call a model and are `paid`. A check never aborts the run: an exception or timeout is reported as `fail`. The text report has one line per check followed by its remedy; `--json` emits the full report. `doctor` exits `0` when no check failed (warnings and unsupported items are not failures) and `1` otherwise.

Exit codes: `0` all required nodes done and final integration complete (or application succeeded); `1` execution/store failure (or application dirty-overlap abort); `2` invalid input/config; `3` no eligible agent; `4` cancelled; `5` verification failed (including post-merge application verification); `6` blocked/needs input or exhausted budget; `7` approval required: a privileged operation (`apply`, `reply`, `cancel`, `resume --raise-budget`) was prepared but not confirmed, because no interactive prompt or host permission flow was available and `--yes` was not given. Exit `7` changes no state, and its output prints the exact command to confirm it. For mixed outcomes choose cancellation, then verification failure, execution/store failure, no eligible agent, blocked, and approval required in that order. The `task_apply` / `task_input` tools return the same condition as `status: "approval_required"`. `--json` includes run state and per-node reasons so mixed failures remain inspectable.

### 14.1 Conversational terminal interface (phase 2c)

Bare `relay` opens the TUI only when standard input and output are interactive terminals. `relay tui` explicitly selects it, and global `-C` selects the repository (`relay -C PATH tui`). `relay tui --session SESSION` and `relay resume --session SESSION` reopen a saved session; a run positional argument and `--session` are mutually exclusive. Reopening displays state and recovery requirements without automatically dispatching. Bare non-TTY invocation preserves its existing usage behavior; explicit TUI invocation without a terminal returns a clear usage error. Existing subcommands, JSON output, exit codes, `--help` and `--version` keep their headless behavior and do not import or initialize Textual.

The main layout is a conversation and multiline composer at the center, a task sidebar showing dependencies, agent/model and state, and a task/handoff inspector. At wider terminals the inspector may remain beside the conversation; at 80×24 the sidebar and inspector become keyboard-switchable views so the composer and pending action remain usable. Resizing retains focus, drafts and selection. Color is supplementary: states, verification outcomes and controls have text labels. All operations are reachable without a mouse, with a visible focus order, command palette and discoverable shortcut help.

The conversation displays operator intent, concise coordinator explanations, plan cards, questions, checkpoint summaries and review findings. It does not replay every agent token into the main timeline. Plan review shows graph/dependencies, scopes, candidate agents/models, effective permissions, checks, limits, pinned base and selected context; approval is an explicit action on that revision. Every revised graph shows its change summary and requires approval. The inspector exposes exact recipient handoffs, source links, changed files/diffs, verification evidence and retained agent logs. The coordinator selector lists only qualified Claude/Codex modes and explains unavailable ones (section 7.4).

The composer distinguishes a typed answer to an active question from a new steering request. Submitting steering uses **Checkpoint and revise**: show that admission is pausing, offer to let active work finish or explicitly cancel it, then review the new plan. Merely typing or navigating does not change execution. Structured answers can be submitted while independent tasks continue; stale questions remain visible but cannot be answered as if current. Limit exhaustion presents the retained answer and the required explicit policy change rather than silently extending attempts.

Projection states distinguish idle, coordinating, awaiting answers, awaiting plan approval, executing, checkpointing, stopping, interrupted and ready for review/delivery. Several node states may coexist beneath one projection; a waiting node does not make unrelated execution appear idle. A stop/close action follows section 8.7 and shows **Stopping** until drainage finishes. There is no **Detach** action in this phase. Historical sessions are selectable; switching away from an executing session requires stopping it first, while inspecting its tasks does not. Session deletion is a confirmed action using section 6's reference/ownership rules.

Delivery review presents the cumulative diff, required checks and their outcomes, dirty-overlap information, target branch and prepared-operation confirmation. **Apply** calls the shared delivery service, never a direct Git update. A proposed follow-up can continue from an unapplied retained snapshot; the UI shows that the user's primary branch is still unchanged.

---

## 15. Tech choice and layout

Python 3.11 or newer for the core and the CLI; TypeScript only for the OpenCode plugin. Libraries: `pydantic` v2 for specs (exported to JSON Schema), `PyYAML` for parsing, an async adapter protocol with `subprocess` supervision, and direct `git` invocations for worktrees. Packaging with `uv` (`pyproject.toml`, console script `relay`); tests with `pytest` (discovery accepts `test-*.py` alongside the existing underscore names), lint and format with `ruff`, types with `pyright` in strict mode (state and permission types use `Literal` unions). Process supervision is defined in section 7.1 and command resolution in section 7.2.

Phase 2c adds Textual as a standard Python runtime dependency with lazy UI imports; ordinary CLI startup and headless commands do not initialize it. Pin a compatible range against the project's supported Python matrix. Textual supplies layout, widgets, keyboard interaction and Pilot testing; no Node dependency is introduced for the CLI/TUI. The [Textual worker lifetime](https://textual.textualize.io/guide/workers/#worker-lifetime) follows its owning UI node, so a screen/widget worker must not own Relay execution. The application service has the explicit lifetime and shutdown path in section 8.7. A prompt-toolkit REPL would suit a simpler prompt loop, but the conversation/task/inspector product uses Textual. ACP and native agent terminal embedding are not prerequisites.

Phase 2b's supervisor requires platform lifetime ownership and target locks (section 6). They use only the standard library and `ctypes` (`fcntl.flock` on Unix, `LockFileEx` on Windows), so there is no native binding to build or package. Support is capability-tested on the actual OS and filesystem rather than assumed: unsupported lock semantics disable supervisor execution. Phase 2c separately reuses this binding for session ownership and fails closed when that capability is unavailable; it does not require supervisor IPC or completed native run-lock integration. Phases 1 and 2a retain the interim ownership mechanism described in section 6. The phase-0 spike (`tests/test_locks.py` and `tests/test_ipc_spike.py`) establishes the supported matrix below; qualification is not inferred for other platforms/filesystems.

| Platform (verified) | Locks | IPC |
|---|---|---|
| Windows 11 10.0.26100, Python 3.11, NTFS | `LockFileEx` through `ctypes`. Released immediately when the holder is killed. Not inherited by workers in any `subprocess` mode. Exactly one winner in an 8-way cold start. A held lock file cannot be deleted. | Named pipe only (this Python has no `AF_UNIX` on Windows). asyncio's default pipe grants read to `Everyone` and `Anonymous`, so the pipe is created with an owner-only DACL, `PIPE_REJECT_REMOTE_CLIENTS` and `FILE_FLAG_FIRST_PIPE_INSTANCE` by subclassing asyncio's `PipeServer` (private API: pin Python minor versions and test on each). The peer PID comes from `GetNamedPipeClientProcessId`. |
| Linux (WSL2 kernel 6.18), Python 3.12, ext4, tmpfs and 9p | `fcntl.flock`. Same results on all three filesystems. | `AF_UNIX` stream socket, mode `0600`, peer pid and uid from `SO_PEERCRED`. `sun_path` is about 108 bytes, so deep run directories use the hashed short-path fallback of section 6. |
| macOS, Python 3.11 or newer | Not verified (best effort): `flock` expected to behave as on Linux. | Not verified: peer credentials would use `LOCAL_PEERCRED`; `sun_path` limit 104. |

Two rules follow from the spike. Workers are started by exec only (`subprocess`), never by a bare `os.fork()`, because a forked child shares the open file description and keeps a Unix `flock` alive after its owner dies. Lock files are never unlinked by `relay clean`: Unix allows deleting a held lock file, after which two contenders could lock different inodes. Network filesystems (NFS, SMB) are not verified; `doctor` probes the actual filesystem before native ownership is enabled.

```
pyproject.toml            uv project; console script: relay
src/relay/
  core/     registry.py classifier.py router.py model_router.py route_log.py
            store.py runrecord.py events.py run.py runlock.py recovery.py permissions.py env.py
            gitbase.py gitexclude.py worktree.py capture.py verify.py publish.py apply.py
            doctor.py answers.py yamlio.py
            graph.py planner.py scheduler.py scopes.py snapshots.py
            specs/{common,task,result,graph,answers,route}.py
            platform/{locks,proc,procid}.py
  adapters/ base.py parsing.py fake.py fake_cli.py fake_scenario.py
            claude.py codex.py antigravity.py
  cli/      main.py phases.py exitcodes.py errors.py commands/*.py
  tui/      phase 2c: app, conversation/task/inspector views; no execution logic
plugin/                   phase 3 TypeScript front-end; gateway additions in phase 5
schemas/*.json            generated from relay.core.specs
docs/  tests/
```

Planner, scheduler and IPC modules belong to their owning phases in section 16. Phase 2c adds core session/controller, coordination/context contracts and shared delivery-service boundaries, plus optional adapter capabilities; `tui` only projects their state and submits commands. An adapter module does not by itself make that agent eligible for routing. The default foreground registry and first TUI workers are Claude and Codex; `fake` is injectable for tests. Antigravity routing integration belongs to phase 4.

Single-source specs: pydantic models in `relay.core.specs` generate `schemas/*.json` through a build step (including `schemas/route-entry.json`), used both for runtime validation and for the CLIs' `--json-schema` (inline JSON string) / `--output-schema` (file path). The plugin derives its tool argument schemas from the generated files, so there is no second definition.

---

## 16. Roadmap

The MVP is cut as a thin vertical slice that proves the core loop (route, run one CLI in an isolated worktree, capture, verify, deliver) before any concurrency or background machinery is built. Each phase ends with a usable tool.

| Phase | Name | Deliverable | Explicitly not included |
|---|---|---|---|
| 0 | Foundations | Spec schemas (pydantic to JSON Schema), `agents.yaml` loader with merge/validation, run store and `events.jsonl`, `fake` adapter, `relay doctor` basics, repository-local exclude block. Spikes that settle open design questions: the lock and named-pipe behaviour matrix (section 15) and real-CLI behaviour canaries (section 7). | Any real agent run |
| 1 | **MVP: foreground single node** | `relay run` (foreground, one node, one CLI process): rule classifier, router with `routes.rules`, ModelRouter, route log (`routes.jsonl`, `route.log`) and `relay routes`/`explain`; Claude and Codex adapters; worktree capture, verified output commit, final integration branch, default Verifier; permission `full` by default plus `readonly`/`workspace` only where a verified profile passes `doctor` (Windows: `full` only for adapters without one); environment allowlists; interim exclusive-create run lock (section 6); `relay status/result/agents/doctor/clean`; git-mode `relay apply` (prepared-tree verification, overlap check, expected-ref update, `--branch`; no journal). Non-git directories are refused with `git init` instructions. **`needs_input` in foreground mode** (typed questions, inline terminal answers, or `relay reply` plus `relay resume` for non-interactive runs; section 8.2). | Task graphs, concurrency, detached supervisor, IPC, native lock, IPC-routed replies to a background run, shadow-git, OpenCode plugin, Antigravity/Gemini/Cursor, cost ceilings, `spawn_requests` |
| 2a, 2b | Graphs and supervisor | **2a (foreground graphs):** scheduler and task graph, dependency snapshots and merge, conflict resolution node, fallback/escalation, input-key reuse and invalidation, `relay graph/plan/resume/cancel` (foreground). **2b (background):** detached per-run supervisor, native run lock (`flock`/`LockFileEx`, validated by the phase-0 spike), authenticated IPC with observer/privileged split, crash recovery and attempt reconciliation, background `needs_input` and `wait`. 2b ships only on qualifying platforms. | OpenCode plugin, shadow-git, cost ceilings |
| 2c | Foreground interactive coordination | Python Textual conversation/task/inspector UI; session ownership/store; bounded, restricted CLI-backed coordinator using Claude or Codex; approved immutable plans and revised rounds; active answers; visible structured handoffs; checkpoint/resume; unapplied follow-ups and cumulative prepared delivery (sections 5.6, 7.4, 8.7, 14.1). | Detached lifetime/IPC, worker-created dynamic DAGs, Cursor/other TUI workers, provider gateway, monetary ceilings |
| 3 | OpenCode plugin | Router tools (`route_task`, `run_agent_task`, `run_task_graph`, `route_log`, `task_*`), companion agent/commands, event mirroring, host-approval flow for privileged tools. Depends on 2b. | Provider gateway |
| 4 | More agents and delivery modes | Antigravity/Gemini and Cursor adapters, `spawn_requests`, cost reservations/accounting (advisory mode; hard mode only if a bound is proven), shadow-git mode with journaled workspace sync and conflict manifests, `replay`. | |
| 5 | Gateway and advanced planning | Provider gateway (surface 2), optional API/gateway-backed classifier and advanced planner. The bounded CLI-backed coordinator belongs to phase 2c and does not depend on this gateway. | |

Phase 2c builds on the foreground graph primitives in 2a and the existing platform lock binding for session ownership. Implementation can proceed without waiting for phase-2b supervisor work or prior milestone release sign-off; this does not waive release gates. Interactive release requires qualifying coordinator profiles and native session locking on the supported OS/authentication matrix, the phase-2c test areas below, and applicable project release checks. Unverified profiles stay unavailable. Describing phase 2c here does not enable commands or change the implementation's phase gate; commands and schema capabilities are enabled only with their implementing changes.

**Command availability by phase** (the CLI rejects later commands with a clear "available from phase N" error):

| Phase 1 | Phase 2a / 2b / 2c | Phase 3 | Phase 4+ |
|---|---|---|---|
| `run`, `status`, `result`, `apply`, `reply`, `resume` (single node), `explain`, `routes`, `agents`, `doctor`, `clean` | 2a: `plan`, `graph run`, `cancel`, graph `resume`; 2b: IPC replies and `wait`; 2c: interactive bare `relay`, `tui`, `resume --session` | `opencode-config` and the plugin tools | `replay`, `--raise-budget`, shadow-git `apply`, cost features |

### Testing strategy

Tests are organized by area; the phase in brackets says when each area must be green. Adapters and the Verifier are tested against the `fake` adapter and recorded CLI outputs first; live CLIs are optional (`RELAY_LIVE=1`).

CI checks the minimum supported Python and the latest stable Python 3 release on Windows and Linux; macOS uses the latest stable release as a best-effort job. Resolve the exact interpreter with `actions/setup-python`, pass that resolved version to `setup-uv`, and key dependency caching from `pyproject.toml` because the lock file is generated locally. CI runs the test, lint, formatting and type checks without enabling live-agent calls.

Offline checks use fake workers or recorded captures; they neither contact a model nor establish a live sandbox profile. To opt into installed, authenticated Claude and Codex smoke tests, set `RELAY_LIVE=1` in the invoking environment and run `uv run pytest -q tests/test_claude_adapter.py tests/test_codex_adapter.py -k live`. These checks can call a model. Record a live result separately from fixture replay and local platform checks; missing CLIs still cause a skip.

- **Config and routing [1]:** built-in, user, project merge order; recursive maps; keyed rule merging by `id` (`enabled: false`, `priority`, stable order within a priority); primitive array replacement; retained `false`/empty values; invalid nulls and anonymous/duplicate ids; rule predicate conjunction/disjunction and comparison operators; higher-priority project exceptions; exhausted and busy preference lists; pinned-agent queueing; `risk` derivation and its complexity bump; `language` resolution; a rule using `cost_ceiling` is rejected before phase 4; escalation defaults (agent without `escalate_on_failure`, single-tier agent, top-tier ceiling).
- **Route log and events [1]:** the full routing entry is durable in `events.jsonl` before dispatch (kill the worker immediately and the entry still exists); exactly one entry per attempt, escalation and fallback with correct `event`, `from`, `agent_rule`, `model_rule`; implicit rule ids when no rule matches; `previewed` entries have `executed: false`; `rerouted` on resume with changed inputs; title truncation, secret redaction and `include_goal: false` across canonical events and both projections; a projection write failure warns without failing the run and missing projections are rebuilt from events idempotently by `seq`; `route.log` lines match `routes.jsonl`; `relay routes` filters across runs; catalog completeness (node state transitions, attempt phases, apply steps, no answer text).
- **Adapters [1, 4]:** `buildInvocation` snapshot-tested against the flag table; `parseResult` tested on recorded outputs (golden files); `--bare` omitted for subscription logins; session-id canary per adapter with fresh-session fallback; Windows `.cmd` shim spawning and quoting canary, including metacharacters and environment-variable syntax remaining literal, rejected newline arguments, and multiline stdin prompts; environment allowlist survival (Windows `SystemRoot`/`PATHEXT`/`APPDATA`, Unix `PATH`/`HOME`/`XDG_*`, proxy/CA variables, toolchain roots, SSH variables) with proxy URLs redacted from logs and no supervisor secrets in worker environments.
- **Worktrees, capture and verification [1, 2a]:** worktree isolation; uncommitted, staged, untracked, deleted, renamed, binary, symlink and agent-committed changes survive capture and final integration; failed verification publishes no commit; verification cannot alter the published tree; inspection of file-output results exposes finalized changes and verification; the user's checkout is unchanged; omitted `verify` selects defaults, scope-only acceptance still runs defaults, missing executable checks are rejected, explicit skips carry a reason, `owns` vs `diff_scope` boundaries; reserved runtime paths (`.agents/runs/`, `.git/**`) are rejected in specs while `.agents/agents.yaml` edits are accepted without changing the running config; the local exclude block exists exactly once, `git status` and `git add -A` show no runtime state and `.gitignore` is untouched; runtime state is excluded from baselines, dirty checks and capture.
- **Permissions [1]:** most-restrictive-wins property tests; disposable canaries for allowed paths, denied external writes through subprocesses and symlinks, read-only repository immutability, declared output exceptions; a CLI plan mode that can mutate through an enabled tool fails the `readonly` profile; unsupported profiles fail closed (Windows `workspace`/`readonly` reported unsupported without a qualifying profile); no test treats `--yes` or peer credentials as proof of human origin.
- **Delivery, `relay apply` [1 git mode, 4 shadow-git]:** a textually clean merge that fails tests never touches the checkout; changed target refs invalidate approval; a working file restored to baseline does not permit overwriting a separate staged edit; disjoint dirty edits are preserved and overlaps abort with a path list; `--allow-dirty` baseline-aware overlap recognition (pre-existing base edits are not conflicts); `--branch` leaves a dirty checkout untouched; sensitive-path scan and `--exclude`; reapplying to another target is a separate operation; exit code `7` changes no state and prints the confirming command. Phase 4 adds shadow-git three-way sync (adds, deletes, renames, binaries, symlinks, modes), unique conflict manifests, crash injection before and after every mutation and before `apply.finished`, journal recovery, and concurrent user edits blocking recovery.
- **Ownership and recovery [1 interim lock, 2b native]:** phase 1: exclusive-create lock, live owner refused, dead owner detected by PID plus start identity, an `interrupted` run re-run. Phase 2b: simultaneous cold starts and stale-socket recovery under the native lock; stale heartbeat while the owner lives; PID reuse; unsupported platform or filesystem fails before writes; descriptors not inherited by workers; purge cannot replace lock objects; owner death during capture/verify/publish; stale attempt results rejected; supervisor survives front-end disconnect; the IPC observer token cannot run privileged commands; privileged commands from a worker tree are rejected (best effort); operation-digest mismatch and missing host approval are rejected as workflow cases.
- **`needs_input`, foreground [1]:** the adapter prompt and result schema produce typed `open_questions`; a TTY run prompts inline, records answers with provenance `operator-tty`, and starts a fresh-session attempt with the node worktree preserved; a defaulted question accepts Enter; `:abort` exits with code 4; non-interactive runs persist `blocked`, release the lock, exit 6 and print the `relay reply` command; `relay reply` rejects stale revisions and duplicate submission ids idempotently, requires confirmation or `--yes`, and `--resume` re-runs the node under the interim lock; the blocked attempt counts against `max_attempts` and cost; waiting time is excluded from active duration; exhausted limits keep the node blocked until `relay resume --max-attempts N`; a free-text question in a `done` result stays advisory; answer content never appears in `events.jsonl`; the `doctor` canary checks that Claude and Codex return `needs_input` for a deliberately ambiguous task.
- **Graphs, resume and input [2]:** ordering, concurrency caps, failure policies and cancellation with a fake adapter; invalid/cyclic graph mutations; total retry limits across fallback and resume; blocked exit codes; overlap tests with narrow `owns`, broader `diff_scope` and missing scope declarations; resolved-diamond-then-fan-in merge ancestry and snapshots preserved after pruning; conflict node bounded to one attempt; snapshot/resume (a review after a writer sees its dependency output; diamonds preserve shared ancestry; disconnected terminal writers appear in final integration; changing an upstream commit, result, artifact, instruction, config or verifier version invalidates descendants while independent nodes stay reusable; missing outputs prevent reuse); `needs_input` in graphs and background runs (the shared rules above also hold): last-attempt input preserves answers and stays blocked until an explicit limit increase, duplicate/stale revisions handled deterministically, answers change the input key and force a fresh session, waiting time excluded from execution duration.
- **Budgets and pricing [4]:** token-to-USD conversion with disjoint buckets and no double charging; omitted cache rates labeled `estimated`, disqualifying hard mode, with the `doctor` warning; estimation fallbacks; concurrent reservations cannot oversubscribe; retries, fallback and planner calls are charged; settlement and restart are idempotent; unknown charges retain reservations; advisory overruns stop admission; hard mode rejects adapters without enforceable bounds.
- **Windows [1]:** short worktree root outside the repo, a 300-character path round-trips, truncated node ids stay unique and map back through `run.json`, `doctor` fails with a remedy when long paths are unavailable.
- **OpenCode plugin [3]:** tool handlers against a mocked `client`, including `wait: false` non-blocking return, wait expiry returning the actual state without cancelling, explicit session abort cancelling, and `approval_required` results; one manual end-to-end in OpenCode.
- **Session ownership and approval [2c]:** concurrent opening/recovery while planning or waiting for approval admits one owner; non-inherited native locks and unsupported filesystems; linked-run CLI commands honor session ownership; crash injection before/after approval intent, run allocation and first dispatch cannot launch a duplicate round; stale/superseded approvals fail; reopening never auto-dispatches; deletion/purge cannot remove referenced checkpoints or active runs; old unlinked run records and headless commands remain compatible.
- **Coordination and context [2c]:** fake coordinator responses cover the closed action union, malformed responses and one-repair bound, step/duration limits, cancellation, unavailable profiles, and explicit coordinator selection; per-version/OS/auth/config opt-in canaries cover startup customizations, tools and read-only enforcement without changing auth; no `full` or provider fallback. Manifests reproduce exact recipient payloads, enforce byte/estimated-token budgets and hash/range provenance, label omissions, reject runtime/auth/traversal/symlink escapes, preserve transitive dependency checks, and never promote logs or worker text into policy.
- **Live input and revised rounds [2c]:** answering one blocked node while an independent worker runs, duplicate/stale command revisions, limit exhaustion, correct descendant reopening and fresh CLI sessions; durable pause wins races with answers/retries; finished workers contribute only dependency-closed successful outputs; canceled edits remain diagnostic; conflicts require bounded approved repair. Follow-ups pin an unapplied snapshot rather than moving HEAD; cumulative delivery verifies prior checks and scope even when the last round narrows them; target changes invalidate prepared confirmation.
- **Interactive lifetime and progress [2c]:** slow subscribers and full queues cannot block pipe draining/control; terminal escapes, markup and secrets are inert/redacted; streaming preserves final result parsing or reports unsupported mode; subscriber replay is idempotent. Screen changes do not own execution; app shutdown, all-task cancellation, two real SIGINTs and owner death during coordinator/worker/verification/merge phases retain ownership until actual drainage, preserve edits and prevent late publication; unknown termination blocks resume.
- **Terminal UX [2c]:** Textual Pilot keyboard workflows for prompt, plan approval, live answers, checkpoint/revise, source inspection, diff/apply and session reopening; responsive 80×24 and 120-column layouts, focus/draft preservation, visible states without color, Windows/Linux terminal checks. CLI import/entry tests preserve non-TTY, existing subcommands, JSON, help/version and no-Node operation. Live CLI and enforcement canaries remain opt-in; fixture replay is not live capability proof.

---

## 17. Risks and open questions

| Risk / question | Mitigation |
|---|---|
| CLI flags change between versions | Adapters are small and snapshot-tested; `relay doctor` canary runs; version ranges recorded in the registry. |
| Structured-output flags differ per CLI and change between versions | Verified against Claude 2.1.308 (inline schema without `$schema`, result in `structured_output`) and Codex 0.163.0 (strict-form schema only, section 5.4); recorded outputs are golden tests (`tests/fixtures/recorded/`); `relay doctor` canary checks the output is schema-valid, otherwise the adapter falls back to a prompt-injected schema. |
| Windows `.cmd` shims (Cursor `agent`) and quoting | `resolveCommand()` per adapter, prompts via stdin/file, quoting canary in `relay doctor` (section 7.2). |
| Antigravity/Gemini availability and model slugs depend on the user's plan (Antigravity replaced Gemini CLI for unpaid-tier and Google One users on 2026-06-18; `agy models` slugs vary with plan quotas) | The adapter is optional; `probe()` records availability and the model slugs; routing falls back to other agents when unavailable. |
| Unresolved: whether a third-party tool may launch the official `agy` binary in headless mode under the user's own login (forum threads Sep/Oct 2026; a Google forum respondent in Sep 2026 recommended an AI Studio API key as the primary provider and local `agy` headless execution only as an optional, local-only provider, without a definitive policy statement) | `relay` only spawns the user's installed CLI and never reads or reuses its OAuth token (same as Claude/Codex); recorded as an open risk. |
| Antigravity headless soft-denies tools that would prompt, continues and exits 0 with a notice on stderr | Exit code 0 is never treated as success on its own; the adapter parses the envelope `status` and `error`, and write nodes are finalized only after verification (section 7.3). |
| Codex `exec` approval/sandbox behaviour inconsistently documented | Verify with a canary at probe time and record the observed behaviour; do not rely on docs alone. |
| Read-access nodes (research, review, debug, architecture) have no eligible agent while every adapter advertises `full` only | Fail closed is kept; spike 0.12 and task 2.30 qualify a `readonly` profile per OS before M3 sign-off; routing reports the unmet level instead of widening it (section 9.3). |
| No qualifying restricted coordinator profile exists for a supported CLI/OS/auth mode | Spike 2.31 returns go/no-go before the coordinator loop is built; without a qualifying mode, coordination is unavailable there and the headless `plan` templates remain (section 7.4). |
| Default `full` permission raises blast radius | Worktree per writer, clean-tree check, env allowlist, banner and event record; restriction flag is one argument. |
| Parallel edits conflict | Effective allowed write scopes, conservative serialization on overlap, bounded conflict resolution preserving merge parents, integration verification. |
| Worktree disk, inode and git-metadata accumulation | Intermediate worktrees are removed with `git worktree remove` and `prune` after verified merge, failed ones are kept for inspection, `relay clean` prunes the rest (section 6). |
| Parallel build/package cache lock contention | Worktree manager isolates build target paths (e.g. `CARGO_TARGET_DIR`) or serializes cache-mutating phases when flagged. |
| Agent claims success falsely | Verifier defaults to executable checks; complete delta capture and orchestrator-owned commits; explicit skips are labeled. |
| Stale review or resumed result | Pin dependency snapshots for all nodes; hash complete inputs and validate stored outputs before reuse; invalidate descendants. |
| Approval settings mistaken for isolation; restricted profiles unavailable (notably native Windows) | Only verified enforcement profiles qualify; unsupported restricted levels fail closed; approval modes and post-run immutability checks do not count (section 9.3). |
| Cost runaway, or costs that look exact | Phase 4 reservations and cumulative accounting; advisory limits disclose overshoot, hard mode needs enforceable bounds, defaulted cache rates are `estimated` and never satisfy hard mode; earlier phases reject configured monetary ceilings (section 8.5). |
| OpenCode tool execution timeouts | Default `wait: false` and a capped `wait_timeout_s`; poll `task_status` (sections 8.6, 13.1). |
| Compromised or injected agent abusing supervisor IPC or the user's branch | Same-user `full` workers are not isolated by prompts, tokens, peer checks or `--yes`; operation-scoped confirmation guards normal operator workflow, while stronger isolation needs a verified sandbox or separate broker (section 8.6). |
| Run/session ownership and double admission | Foreground phases 1/2a retain interim run locks; phase 2c uses native session ownership, ordered session/run acquisition and recoverable approval-to-run intents; phase 2b separately integrates native supervisor/run locks and IPC. Unsupported session locks disable interactive ownership without disabling legacy foreground commands (sections 6, 15). |
| MVP scope creep | The MVP is a foreground single-node slice with a fixed "not included" list; later features are rejected with an "available from phase N" error rather than partially implemented (section 16). |
| Claude `--bare` breaks subscription (OAuth) login | `probe()` records the auth mode; `--bare` only with API-key auth (section 7.2). |
| Delivery to the user's branch: dirty trees, `--allow-dirty`, unapplied results | Target-specific verification, preserved disjoint dirty edits, journaled recovery (phase 4 shadow-git), unique conflict manifests, `relay apply` and `task_apply`, exit code `7` prints the confirming command (sections 8.3, 14). |
| Environment allowlist breaks child processes or corporate networks | Platform essentials, proxy/CA variables and CLI config homes are in the base allowlist (section 9.4). |
| Runtime state pollutes the user's `git status` or gets committed | Marked block in the repository-local exclude file (`info/exclude`, never `.gitignore`), orchestrator path filters, `doctor` check (section 6). |
| Windows path-length failures in nested worktrees | Short worktree root outside the repo on Windows, per-worktree `core.longpaths`, `doctor` long-path canary (section 8.3). |
| `needs_input` depends on the agent following the result-spec protocol | Prompt and typed schema instruct agents to stop and ask; the `doctor` canary checks an ambiguous task yields `needs_input` (in the phase-0 spike both Claude and Codex returned typed questions on the first try, a single run each, so reliability is not yet measured); free-text questions in `done` results remain advisory, so a non-compliant agent degrades to guessing rather than blocking (section 8.2). |
| Session ids: legacy Gemini has no headless resume; Antigravity `conversation_id` resume is documented but context retention is unverified | Claude, Codex and Cursor confirmed; per-adapter canary; fresh-session fallback with captured state (sections 6, 7.3). |
| `--allow-dirty` snapshot captures sensitive untracked files | Sensitive-path scan and confirmation, `--exclude`, refs never pushed and purged with the run (section 8.3). |
| Route log leaks goal text, or loses decisions on write failure | Redaction and `include_goal: false`; the decision is durable in `events.jsonl` before dispatch and projections are rebuilt (section 4.4). |
| Provider gateway fidelity | Documented as lossy; surface 1 recommended; gateway behind a flag. |
| OpenCode plugin API drift (e.g. `config` hook, subagent session creation) | Isolate OpenCode-specific code in `plugin/`; test against a pinned version range. |
| Coordinator planning exceeds approved intent or inherits unsafe CLI customizations | Restricted, capability-qualified Claude/Codex coordinator; bounded actions/context; initial and every revised graph require operator approval; no unrestricted/provider fallback (sections 5.6, 7.4, 8.7). Deterministic templates remain the headless `plan` behavior. |
| Interactive UI obscures execution or outlives safe ownership | Textual conversation/task/handoff views over the shared service; foreground close drains before unlocking, progress never controls execution, and session projections distinguish waiting/stopping from idle (sections 11, 14.1). Background survival remains phase 2b. |
| Follow-up rounds lose earlier checks, scope or unapplied changes | Pinned retained bases, checkpoint/import provenance and cumulative session delivery policy; shared CLI/TUI apply verifies the final prepared tree against that policy (sections 5.6, 8.7, 10). |

---

## 18. References

- Claude Code headless: https://code.claude.com/docs/en/headless
- Claude Code CLI controls and restrictions: https://code.claude.com/docs/en/cli-reference
- Claude Code permissions and sandboxing: https://code.claude.com/docs/en/permissions#how-permissions-interact-with-sandboxing
- Codex CLI reference: https://developers.openai.com/codex/cli/reference
- Gemini CLI headless: https://google-gemini.github.io/gemini-cli/docs/cli/headless.html
- Antigravity CLI headless: https://antigravity.google/docs/cli/headless/
- Antigravity CLI reference: https://antigravity.google/docs/cli/reference/
- Cursor CLI parameters: https://cursor.com/docs/cli/reference/parameters
- OpenCode plugins: https://opencode.ai/docs/plugins/
- OpenCode agents: https://opencode.ai/docs/agents/
- OpenCode providers: https://opencode.ai/docs/providers/
- OpenCode SDK: https://opencode.ai/docs/sdk/
- OpenCode custom tools: https://opencode.ai/docs/custom-tools/
- Agent protocol survey: https://arxiv.org/abs/2505.02279
- Native ownership locks: [Linux `flock`](https://man7.org/linux/man-pages/man2/flock.2.html), [Windows `LockFileEx`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex).
- IPC transport lifecycle: [Python asyncio Unix sockets and Windows named pipes](https://docs.python.org/3/library/asyncio-eventloop.html#creating-network-servers) (transport discovery is separate from native ownership locking).
- Textual: [getting started](https://textual.textualize.io/getting_started/), [worker lifetime and cancellation](https://textual.textualize.io/guide/workers/), [Pilot testing and terminal sizes](https://textual.textualize.io/guide/testing/).
