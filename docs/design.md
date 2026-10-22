# Agents Coordinator: Multi-Agent CLI Router

Status: Draft · Scope: programming tasks

This document is the specification: it states each rule once, in the section named in the map below, and other sections refer to it. Change history is in [`revision-changes.md`](revision-changes.md), the task breakdown in [`development-plan.md`](development-plan.md), and the rule for which file holds what in [`AGENTS.md`](AGENTS.md).

A router that takes software-engineering tasks, decides which agent CLI (Claude Code, Codex, Gemini CLI, Cursor, ...) and which model should do each one, runs them as concurrent sub-agents, and passes context and results between them as specs (files). It ships as two front-ends over one core: a standalone `orch` CLI and an OpenCode plugin. The core and the CLI are Python; the only TypeScript is the OpenCode plugin, which OpenCode requires to be JS/TS (section 15).

### Document map

| Topic | Canonical section |
|---|---|
| Goals, assumptions, phase scope | 1, 16 |
| Components and process model | 2, 8.6 |
| Registry, merge and rule semantics, pricing, risk, paths | 3, 4.1 |
| Agent and model routing, route log | 4 |
| Spec formats (task, result, input answers) | 5 |
| Run store layout, ownership/locks, recovery, runtime-state exclusion, reuse keys | 6 |
| Adapters, per-CLI flags, session ids, Windows spawning | 7 |
| Task graph (8.1), scheduling and the `needs_input` protocol (8.2), isolation/merge and delivery/`apply` (8.3), spawn requests (8.4), cost accounting (8.5), supervisor, IPC, wait and cancel semantics (8.6) | 8 |
| Permission levels, enforcement, environment allowlist, injection hygiene | 9 |
| Verification | 10 |
| Events and observability | 11 |
| OpenCode plugin, `orch` CLI, layout | 13, 14, 15 |
| Roadmap, tests, risks | 16, 17 |

### Phase availability

Sections describe the full design. Not everything exists from the first release: section 16 lists what each phase delivers, and the MVP (phase 1) is a deliberately thin slice: a foreground, single-node `orch run` with routing, the route log, worktree capture and verification, and git-mode `orch apply`. Features that arrive later are tagged in place (for example "phase 2" or "phase 4") and are rejected as unsupported, rather than silently ignored, before they exist.

---

## 1. Goals, non-goals, assumptions

### Goals
- Route programming tasks (implement, bugfix, refactor, test, review, investigate, migrate, docs-for-code, research, design) to the best-fit agent CLI.
- Per-agent **model routing**: each CLI has its own model catalog and its own rules for choosing a model.
- Run **multiple sub-agents concurrently**, each invoking a different CLI, with dependency ordering (task DAG), fan-out and fan-in.
- Exchange context and output between agents only through **versioned, validated specs** (files), not transcripts.
- **Highest permission by default** for agent CLIs, with an explicit parameter to restrict.
- Two deliverables over one shared core: the `orch` CLI and an OpenCode plugin.

### Non-goals
- Not a new coding agent; the CLIs do the coding.
- Not a general-purpose (non-programming) assistant router.
- No hosted service or multi-machine scheduling in v1 (local processes only).
- No re-implementation of the CLIs' own tool loops, MCP handling or authentication. Users log in to each CLI themselves.

### Assumptions
- Each CLI is installed and authenticated on the machine; the router only discovers and invokes it.
- Python 3.11 or newer is available on the host (Windows, Linux; macOS best effort). Node is needed only to run OpenCode and its plugin, never by `orch` itself.
- Work happens in a git repository (needed for worktree isolation, delta capture, and diff verification). Git is a strict requirement in phases 1 to 3: running in a non-git directory fails with instructions to run `git init`. From phase 4, `orch` can instead prompt to initialize Git or automatically initialize an isolated shadow Git repository under `.agents/shadow-git/` so that worktree and commit verification mechanics remain fully operational. The shadow repository's baseline commit (a snapshot of the current non-ignored files) is created before the run starts, so a freshly created shadow repository counts as a clean tree for the check in section 9.4. In shadow-git mode (phase 4), verified deliverables from the integration commit are synced back into the user's workspace directory via `orch apply <run>` (sections 8.3, 14).
- Flags in section 7.2 were checked on 2026-10-22 against local `--help` output for Claude Code 2.1.308, Codex 0.162.1 and Cursor `agent` 2026.10.19, and against official docs for Gemini CLI (not installed on the reference machine) and OpenCode. Behaviour (as opposed to flag existence) is not verified by `--help`; anything unverified is marked **[verify]** and re-checked by `orch doctor` canaries. The output shapes of section 5.4 and 7.3 were confirmed against real runs of Claude Code 2.1.308 and Codex 0.163.0 on 2026-10-22 (Gemini was not run; Cursor ran with the account login, no API key).

---

## 2. Architecture

```
                      ┌────────────────────────── front-ends ──────────────────────────┐
                      │  orch CLI (orch.cli)          OpenCode plugin (plugin/, TypeScript)
                      │  run/plan/graph/status/...    tools: route_task, run_agent_task, run_task_graph...
                      │                               optional: OpenAI-compatible provider gateway
                      └───────────────┬─────────────────────────────┬───────────────────┘
                                      ▼                             ▼
┌──────────────────────────────── orch.core ──────────────────────────────────────────────┐
│ Intake ─▶ Classifier ─▶ Planner (DAG) ─▶ Router ─▶ Scheduler ─▶ Worker ─▶ Adapter ──▶ CLI process
│                             │               │         │            │          (claude|codex|gemini|cursor)
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
| Router | Picks the agent for each node (section 4). |
| ModelRouter | Picks model and reasoning effort inside the chosen agent using that agent's own rules (section 4.3). |
| Scheduler | Runs ready nodes on a bounded worker pool with limits, timeouts, retries, fallback, cancellation. |
| Worker | One supervised OS process wrapping one adapter invocation in its own worktree. |
| Adapter | Translates a task spec into a CLI invocation and the CLI output back into a result spec. |
| Verifier | Runs the task's `acceptance` commands itself and checks diff scope. |
| Store | Run directory with specs, logs, artifacts, state, event log. |
| Supervisor | Per-run detached process that owns the Scheduler, Workers, Verifier and Store writes, and serves authenticated IPC to the front-ends (sections 6, 8.6). Front-ends are thin clients of it. |
| Applier | `orch apply` / `task_apply`: delivers the verified integration commit into the user's branch or workspace after confirmation (section 8.3). |

Dependency rule: `core` has no OpenCode dependency. The plugin contains no routing or run logic; it reaches the core only through the `orch` executable (`--json` output) and the supervisor IPC (section 8.6). Both front-ends are thin; they share `agents.yaml`, spec schemas and the run store, so a run started by the CLI can be inspected or resumed from OpenCode and vice versa.

---

## 3. Agent registry (`agents.yaml`)

Routing is data, not code. Merge order: built-in defaults → `~/.config/orch/agents.yaml` (user) → `./.agents/agents.yaml` (project). Later files override earlier ones. Maps merge recursively by key; primitive arrays replace the earlier array in full (including `categories`, `strengths`, and `fallback`). Rule lists (`routes.rules` and each agent's `model_routing.rules`) merge by matching rule `id`: rules with matching `id` override properties or can be disabled via `enabled: false`, while new rule IDs are appended (all rules require an `id`). Explicit `false`, `0`, and empty arrays are retained rather than treated as missing. `null` is rejected unless the field's schema explicitly permits it; it does not delete inherited keys. The merged config is validated and snapshotted in the run store. Permission restrictions additionally follow section 9.2.

Rule IDs are required and unique within each source list. A matching ID keeps its inherited position; nested `when` and action blocks (`prefer` or `use`) replace those blocks in full when supplied. `enabled: false` disables a rule before evaluation. Optional integer `priority` (default 0) orders enabled rules descending, with merged list position as the stable tie-breaker. New project exceptions can precede general inherited rules by setting a higher priority. An empty keyed list adds no overrides; disabling inherited rules requires their IDs. Anonymous rules and duplicate IDs are invalid. Rule predicates in `when` blocks are evaluated conjunctively (all specified attributes must match); array values within an attribute match disjunctively (any value matches); numeric attributes (`est_tokens`, `attempt`, `files`) and ordered complexity levels (`low` < `medium` < `high`) support comparison operators (`<`, `<=`, `>`, `>=`, `=`). Pricing rates in `agents.<agent>.pricing` specify USD per 1M tokens; if `cache_read_per_m` or `cache_write_per_m` are omitted, the settlement computation uses `input_per_m` for that bucket as a placeholder; labeling and hard-mode consequences are defined in section 8.5, and `orch doctor` warns when an agent that reports cache tokens lacks explicit cache rates.

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

routes:                        # agent selection rules (section 4.2); first match wins
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
    categories: [codebase-research, docs-for-code, large-context]
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

Capability probing: at startup (and via `orch doctor`) each enabled agent is probed with `<cmd> --version` and `--help`. Missing or unauthenticated agents are marked unavailable and skipped by routing; they never fail a run unless a task pins them explicitly.

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
3. **Score** (`agent_rule: score` or `category-match`): when no rule matches (`routes.default: score`), each candidate from the registry's `categories` and `strengths` gets `fit (category match, strengths) + availability - cost_penalty - recent_failure_penalty - load_penalty`. Highest wins; ties break by `defaults.fallback` order. A single category-only candidate is recorded as `category-match`.
4. **LLM classifier (optional)** (`agent_rule: llm-classifier`): only when rules yield no category or a tie; a single cheap call returning `{category, complexity}`. In OpenCode the host model can provide this directly through `route_task` input.
5. **Fallback** (`agent_rule: fallback`): on adapter failure, timeout, or unavailable agent, retry with the next candidate; record why. A model-tier retry on the same agent is recorded as `escalation`.

### 4.3 Model selection inside an agent

Every agent owns an independent `model_routing` block. The router never shares model rules across agents. The model router receives `(agent, task spec)` and returns `{model, effort, model_rule}`:

- Enabled rules are evaluated in effective priority order (section 3); first match wins; otherwise `default`.
- Inputs: `category`, `complexity`, `est_tokens`, `files` count, `language` (resolved hierarchically: explicit `task.language` -> inferred from dominant extension in `context.files`/`owns` -> `repo.language` default), `risk` (section 4.1), `cost_ceiling` (remaining run budget in USD; available only from phase 4, and a rule that uses it is rejected at config validation before then), `attempt` (retry number).
- Outputs: a logical tier or literal model id, plus optional reasoning `effort`, the matching `model_rule` id (or `default`), and the resolved model pricing basis from `agents.yaml`.
- `escalate_on_failure` (an ordered list of tier names, lowest to highest; when an agent omits it, the default is that agent's `models` tier names in declaration order, and an agent with a single tier never escalates): if a node fails its acceptance checks, the retry moves one tier up the list, subject to budget admission (section 8.5). If the node is already at the highest tier in `escalate_on_failure`, or if the resolved model is a literal ID not present in the tier list, subsequent retries remain at that model until `max_attempts` is exhausted or cross-agent fallback triggers. A cheaper model may be selected when the next attempt would exceed the remaining budget; downgrading alone does not enforce a cost ceiling.
- If the resolved model is unavailable (probe or runtime error) the agent's default model is used and a warning is logged.
- The decision is stored in the task spec (`routing: {agent, model, effort, agent_rule, model_rule}`) and in the route log (section 4.4) with the same field names.

Per-adapter model flags: Claude `--model`; Codex `-m` plus `-c model_reasoning_effort=<level>` **[verify key name]**; Gemini `-m`; Cursor `--model`.

### 4.4 Route log

The router keeps a log of every routing decision: which task went to which agent and model, a short description of the task, and the rule that matched. It answers "why did this task go to that agent" without reading prompts or transcripts, and `orch explain` reads the same entries.

**Files** (per run; the orchestrator is the single writer, as in section 6):
- `.agents/runs/<run-id>/routes.jsonl`: derived, append-only projection, one JSON object per decision; rebuildable from durable routing events.
- `.agents/runs/<run-id>/route.log`: human-readable, one line per decision, written by the same writer from the same entry, e.g.

```
2026-10-22T09:41:03Z #7 n2-impl-a  implement/high  -> codex/deep(high)  agent_rule=route-impl-codex model_rule=codex-deep-hard  "Debounce autosave handler"
2026-10-22T09:47:55Z #9 n2-impl-a  implement/high  -> claude/deep       agent_rule=fallback       model_rule=claude-impl        "Debounce autosave handler"  (from codex: timeout)
```

There is deliberately no shared project-wide file: runs can execute concurrently, and per-run files keep a single writer. `orch routes` (section 14) reads across run directories.

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
| `event` | `routed` (first routing of an attempt), `rerouted` (the routing of a node that already had a decision is recomputed because its inputs changed: `orch resume` with a different `--agent`/`--permissions`, a changed `agents.yaml` snapshot accepted on resume, or new operator answers that change the category/complexity), `fallback` (different agent after a failure), `escalated` (higher model tier, same agent), `pinned` (agent chosen by `--agent`/task), `previewed` (`route_task` dry run, `executed: false`), `rejected` (no eligible agent; `agent` is null). |
| `title` | Short description of the routed task, at most `route_log.title_max_chars` (default 120). |
| `agent_rule` | Matching agent-selection rule: a `routes.rules` id or an implicit id (section 4.2). |
| `model_rule` | Matching model rule id from that agent's `model_routing`, or `default`. |
| `reason` | One-line human explanation of the decision. |
| `alternatives` | Other candidates with scores, for `orch explain`. |
| `from` | For `fallback`, `escalated` and `rerouted`: `{agent, model, reason}` of the previous attempt. |
| `seq` | Monotonic per run, so gaps are detectable. |

**Rules**
- **When written:** for every Router decision: initial routing, each retry with model escalation, each fallback, pinned routes, rejections, and `route_task` previews. The entry is written before the worker starts, so a crash still leaves the record. Reusing a `done` node on resume is not a route (it is a `node.reused` event); re-running a node on resume writes a new entry with `resumed: true`.
- **Short description:** the node's optional `title` (supplied by the planner or the user); otherwise derived from `goal` (first sentence, whitespace collapsed, truncated). It never includes the rendered prompt, file contents, or environment values; strings matching known secret patterns are redacted. With `route_log.include_goal: false`, omit titles and free-text reasons (including `from.reason`) from canonical routing events and both projections; retain operational IDs, agent/model, rules, scores, timestamps, and permissions. Use structured reason codes instead. This setting applies to routing records; prompts, stdout, and result artifacts have separate retention and access policies.
- **Failure handling:** a write failure in either route-log projection raises a `route.log.write_failed` warning and never blocks execution. `events.jsonl` is the canonical source: the complete routing entry is durably appended there before starting an attempt. Failure to persist that event prevents dispatch and reports a store error. On resume, replay missing projection entries idempotently by `seq`; discard only a torn trailing record, never a valid earlier entry. `orch explain` and `orch routes` can read canonical events when a projection is missing.
- **Relation to `events.jsonl`:** `node.routed` carries `{node_id, route_seq, decision}` with the complete entry. Routing fields are authored once in this event and rendered into both projections. `route_log.enabled: false` disables projections, not durable routing events. A pre-start entry's `executed: true` means execution was admitted, not that a child actually started; `node.started` is the evidence of process startup.
- **Retention:** the files live in the run directory and are removed with it (`orch clean --purge-runs`); ordinary worktree cleanup preserves logs and output refs. Active runs are never pruned; no log rotation is needed.

---

## 5. Spec contract

The artifacts below (except `PROJECT.spec.md`) are YAML (JSON accepted), validated by JSON Schemas generated from the pydantic models in `orch.core.specs` (published under `schemas/`). Every spec has `spec_version`.

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

### 5.3 `result.spec.yaml` (worker → router and downstream nodes)

```yaml
spec_version: 1
id: n3-implement-autosave
status: done            # done | failed | needs_input | blocked | cancelled
agent: codex
model: <resolved id>
summary: "Added debounce(300ms) in AutosaveHandler; tests updated."
changes:
  branch: orch/2026-10-22-a1b2/n3
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
spawn_requests:          # optional follow-up work proposed by this node
  - { category: review, goal: "Review debounce edge cases", depends_on: [n3-implement-autosave] }
usage: { duration_s: 212, tokens_in: 0, tokens_out: 0, cost_usd: 0.0 }
session: { cli_session_id: "<id>" }
```

### 5.4 Rules
- **Validation**: every result is validated. On failure the worker's output is returned once with the validation error appended ("fix your result spec"); a second failure marks the node `failed`.
- **Structured output** is requested natively where supported, with the schema generated from the pydantic models. Claude: `--output-format json` with `--json-schema '<inline JSON schema>'`; the adapter removes the schema's `$schema` key, which the CLI rejects, and reads the validated object from `structured_output` (the same JSON also appears as text in `result`). Codex: `--output-schema <file>` with `-o <file>` for the last message; the API accepts only strict schemas, so the adapter first converts the schema (every property listed in `required`, `additionalProperties: false` on every object, each optional property made nullable) and drops the `null` values from the reply before validating it, otherwise the request fails with `invalid_json_schema` before any generation. If a canary shows a CLI does not honour its schema flag, fall back to the schema injected in the prompt instructions. Gemini and Cursor have no schema flag: the adapter instructs the agent to return the result JSON (Cursor returned it as text in `result`; it may also write `result.spec.yaml` in the node directory) and the parser normalises it before validation: `null` values are dropped, and an empty `options` list on a `free` question is removed (Cursor emits both).
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

---

## 6. Context exchange and run store

```
.agents/
  agents.yaml
  locks/                      # persistent native run/target lock objects; excluded from inputs
  apply/<operation-id>/       # durable plan, before-images, progress, conflict artifacts
  shadow-git/                 # isolated objects/staging for non-git workspaces
  runs/
    <run-id>/
      run.json                 # graph, node states, config, input keys, final commit, budget ledger
      run.lock                 # diagnostic supervisor identity: PID, process-start time, generation, heartbeat
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
        input_answers.yaml     # operator answers for a needs_input node (orch reply / task_input)
        prompt.md              # exact rendered prompt sent to the CLI
        stdout.log  stderr.log
        artifacts/
      02-n2-research/ ...
      03-n3-implement-autosave/ ...
  worktrees/
    <run-id>/<node-id>/        # git worktree per node, including readers
```

Durable snapshots live in git, not in this tree: per-attempt output refs and merge refs under `refs/orch/<run>/...` (section 8.3), plus the integration branch `orch/<run>/integration`; they survive worktree pruning and are removed only by `orch clean --purge-runs`.

Rules:
- A worker writes only inside its own `NN-<node>/` directory (plus its worktree). Spec writes are atomic (temp file + rename).
- **Identifiers and records.** A run id is `<UTC date>-<4 hex digits>` (e.g. `2026-10-22-a1b2`), created by exclusive `mkdir` of the run directory (a collision draws a new suffix) and never reused: an id whose lock object under `.agents/locks/` still exists is skipped even after its run directory was purged. Node directories are `NN-<node-id>` with `NN` the zero-padded creation order. `run.json` holds the run `state` (`running | done | failed | blocked | cancelled | interrupted`), `owner_generation`, the config hash, the graph, a record per node (`state` as in section 8.2, directory, blocked reason, attempt count, `input_key`, `output_commit`), the final integration commit and the budget ledger (phase 4). Atomic writes flush the temp file to disk before renaming it; a leftover temp file from a crash is ignored and swept at the next open.
- **Phase 1 (interim ownership, no supervisor):** the foreground `orch run` process is the single writer. It creates `.agents/locks/<repo-key>-<run-id>.lock` with exclusive-create semantics (`open(..., "x")`, i.e. `O_CREAT | O_EXCL`), writes PID and process-start identity into it, and removes it on normal exit. A second process that finds the file checks the recorded PID *and* process-start identity; a live owner means "run in progress, refuse", and a dead owner marks the run `interrupted` and takes over only for `orch run --retry`/`resume`, which re-runs the single node from its last durable state (no mid-attempt recovery is needed because phase 1 has one node and no concurrent writers). Limitations, accepted for the MVP: no attach from a second front-end, and a PID-reuse/start-time race is possible in the interval between the identity check and takeover, which is acceptable because only one user-driven process starts a run. The native lifetime lock below replaces this from phase 2.
- **Phase 2 onward:** the per-run supervisor is the single writer of `run.json`, `events.jsonl`, `routes.jsonl` and `route.log`. Before binding IPC or changing run state, acquire an exclusive native lock on `.agents/locks/<repo-key>-<run-id>.lock`: `flock(LOCK_EX | LOCK_NB)` on supported Unix systems or `LockFileEx` with exclusive/nonblocking flags on Windows, called from the supervisor through the Python standard library (`fcntl.flock`) and `ctypes` (`LockFileEx`); no third-party native binding is used. Keep the lock handle open and non-inheritable until shutdown. A separate helper process that can outlive the supervisor is not an acceptable lock owner. `orch doctor` verifies support on the actual filesystem; unsupported platforms/filesystems fail before execution. Lock objects live outside purgeable run directories and `orch clean` never unlinks/replaces them; contenders must always lock the same object. Purge takes the corresponding run and target locks before deleting records, and run IDs are never reused.
- IPC endpoint existence and connection probes are discovery only, not ownership. A contender unable to acquire the native lock waits for readiness or attaches; failure to connect never authorizes unlinking a socket or launching workers. Only a lock holder may remove a leftover endpoint, bind a fresh endpoint, or recover state. This avoids the stale-socket unlink/rebind race between simultaneous starters. `run.lock` is replaceable diagnostic metadata (PID, process-start identity, generation, heartbeat), never the lock itself. Canonical repository identity plus run id namespaces endpoint addresses; shorten/hash Unix socket paths and Windows pipe names to fit platform limits.
- After owner death and OS-lock release, a replacement supervisor increments the owner generation, reconciles the durable event log and `run.json`, and terminates or proves termination of recorded old worker process groups before retrying them. Use process-start identity, not PID alone, to avoid PID reuse. If termination cannot be established, leave affected nodes `blocked` for intervention; never dispatch duplicate writers. Attempt results carry generation and attempt IDs; stale owners cannot publish results or update refs. Persist successful output refs and a durable completion event before releasing dependents; recovery finishes or rolls back an interrupted publication idempotently.
- Context handed to a node is assembled by the Router from `context.from` plus `PROJECT.spec.md`, under a token budget. If over budget, summaries are preferred over artifacts; the node can read referenced files itself.
- Session continuity: for retries and follow-ups on the same agent the adapter stores `session.cli_session_id` and resumes (Claude `--resume <id>`, Codex `exec resume <id>`, Cursor `--resume`; Gemini headless has none, so context is re-sent). Resume is only used within a node (retry), never across nodes, to keep nodes reproducible and independent.
- Worktree retention and cleanup: Worktrees under `.agents/worktrees/<run-id>/<node-id>/` are preserved during node execution. Upon verified fan-in or final integration merge, worktrees of successful intermediate nodes are automatically decommissioned using `git worktree remove --force <path>` followed by `git worktree prune` (leaving commits and diff artifacts intact in the git object database under `refs/orch/<run>/...` and in the run store) to prevent disk space, inode, and git administrative lock exhaustion. Failed node worktrees are retained for debugging. `orch clean [--older-than <duration>]` prunes old run worktrees.
- Completed-node reuse uses an `input_key` computed from a canonical encoding of the effective task spec, input commit and tree, all dependency output commits and result/artifact content hashes, the rendered context and `PROJECT.spec.md` snapshot, effective configuration (including verification commands and permissions), adapter/CLI versions, and verifier version. Required files and commits must still exist and match their recorded hashes. Secrets are excluded from stored keys; reuse is disabled when an execution-affecting secret changes or cannot be compared safely. Changed inputs invalidate the node and its descendants; dependencies are recomputed in topological order before reuse is considered. CLI session resume is disabled when this key changes.
- Runtime state is never repository input or a deliverable. The **reserved paths** are `.git/**` and the runtime subtrees listed here (section 8.1 refers to this list). Explicitly exclude `.agents/locks/`, `.agents/runs/`, `.agents/worktrees/`, `.agents/shadow-git/`, `.agents/scratch/`, `.agents/apply/`, and recorded legacy conflict artifacts from baseline creation, dirty checks, change capture, candidate materialization, and delivery. Do this in orchestrator path filters without editing the user's tracked ignore files. **Keeping the user's own git view clean:** in git mode, the first run idempotently adds a marked block (`# >>> orch runtime state` ... `# <<< orch runtime state`) listing those subtrees to the repository's local exclude file (the path from `git rev-parse --git-path info/exclude`, which is shared by linked worktrees and is never committed), so `git status` and `git add -A` in the user's checkout ignore runtime state and the nested worktrees are not offered for staging. `.gitignore` is never modified; `orch clean --purge-runs` leaves the block in place, and `orch doctor` verifies it. In shadow-git mode there is no user repository to protect; the shadow store is an isolated Git directory whose staging snapshot applies the same exclusions before its first commit, so the baseline cannot ingest its own objects, logs or worktrees. Validation fails if tracked files collide with a reserved runtime subtree. `.agents/agents.yaml` and human-owned project instructions remain ordinary inputs.

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

The `fake` adapter (tests only) drives `orch.adapters.fake_cli`, a stub agent process that follows a per-attempt scenario file: it writes, deletes and commits files, emits each output mode of section 7.3, returns `needs_input` with typed questions, fails, sleeps or hangs until killed, and records the prompt and environment it received. Scenarios are selected by node and attempt, the last one repeating, so retry, escalation, fallback and `needs_input` paths run without a real CLI.

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

Codex `codex exec --help` also lists `--worktree`; the router never uses it, to keep one worktree owner. The `-a/--ask-for-approval` flag exists only on the top-level `codex` command, not on `exec`, so non-interactive approval behaviour on `exec` is set through the sandbox/bypass flags and `-c approval_policy=...` **[verify]**. The Codex `-p` flag selects a config profile, not a prompt.

The restricted-level entries are invocation hints; what counts as enforcement, and which levels `probe()` advertises, is defined in section 9.3.

Known caveats (tracked as risks): Codex approval-flag behaviour and default sandbox for `exec` differ between third-party sources, so `orch doctor` runs a canary command and records actual behaviour; MCP calls under `codex exec -s read-only` are reported as cancelled; Gemini headless auto-denies anything that would prompt unless yolo or a policy allows it, and its `--allowed-tools` flag is deprecated in favour of the Policy Engine; the Gemini docs also state that Gemini CLI was replaced by Antigravity CLI for unpaid-tier and Google One users on 2026-06-18, so availability depends on the user's plan; Cursor runs unattended with an account login (verified; the login lives in the user profile, so the Windows environment allowlist must keep `USERPROFILE` and `APPDATA`) or with `CURSOR_API_KEY` / `--api-key`; its binary name is `agent` with `cursor-agent` as a legacy alias.

**Windows process spawning:** on the reference Windows machine `claude` and `codex` are real executables (`claude.exe`, `codex.exe`), but Cursor's `agent` and `cursor-agent` are `.cmd` shims. Python's `subprocess` can run a `.cmd` file only through `cmd.exe` and its quoting rules (so arguments need careful escaping), so each adapter resolves the real command through a `resolveCommand()` helper (PATHEXT lookup; for `.cmd`/`.ps1` shims either invoke `cmd.exe /d /s /c` with strict argument quoting or locate the underlying `node`/script) and `orch doctor` checks that a canary prompt containing quotes and newlines round-trips. Prompts are passed through stdin or a file where the CLI supports it (Codex `-`, Claude and Gemini piped stdin) to avoid command-line quoting and length limits.

### 7.3 Output parsing order
1. Native structured output (schema-constrained) if the CLI supports it.
2. `result.spec.yaml` written by the agent in the node dir.
3. JSON/YAML block extracted from stdout.
4. Otherwise a synthesized candidate result: provisional `status` from exit code, `summary` from the last message, `changes` from the complete change capture in section 8.3 (always computed by the orchestrator, never trusted from the agent). Exit code zero alone does not finalize a write node as `done`.

In all parsing modes, the adapter extracts and records `session.cli_session_id` from the CLI output payload to enable in-node session resumption on retries. Field names: Claude JSON `session_id`; Codex JSONL `thread.started` event `thread_id` (both confirmed against real output; `--resume` and `codex exec resume` keep the same id and the earlier context); Cursor JSON `session_id` (confirmed on `agent` 2026.10.19, same behaviour on `--resume`); Gemini none. `orch doctor` runs a trivial canary per adapter, checks that a session id is extracted and that a follow-up resume with it succeeds, and records the observed field names in the adapter profile. If extraction fails, retries run in a fresh session with the prior attempt's captured state passed in the prompt (the same behaviour as Gemini).

---

## 8. Execution model: supervisor with concurrent sub-agents

The router is a supervisor that starts several sub-agents (workers), each invoking a different CLI, according to a task DAG.

### 8.1 Task graph

Validate the complete graph before dispatch and after every proposed mutation: unique path-safe IDs, known categories, existing dependency and context references, no self-dependencies or cycles, and bounded node/depth counts. `context.from` may reference only transitive dependencies. Task specs, `owns` and `diff_scope` must not target or intersect the reserved paths defined in section 6 (`.git/**` and the runtime subtrees); such tasks are rejected during graph validation. The check gives the same answer on every host and errs toward rejecting. It compares path segments after dropping empty and `.` segments, ignores case, and lets `**` stand for any number of segments. A segment matches a reserved name if it could refer to it on Windows: as a glob, with trailing dots or spaces, or with `~` followed by a digit or glob character (a possible 8.3 short name such as `GIT~1`) or a `:` (a possible NTFS stream). A scope is rejected when it can reach a reserved path through at least one segment other than `*` or `**`; scopes made only of those, such as `**` or `*/hooks/*`, cover runtime state only by accident, and the orchestrator filters it out of them (section 6). A path that names the repository root itself (`.`, `./`) is rejected. `.agents/agents.yaml` and project instructions are ordinary repository files and may appear in `owns`/`diff_scope`; a node that edits `.agents/agents.yaml` changes only the delivered result, never the running run (the configuration is snapshotted at run start, section 3). Resolve node access explicitly as `read|write`: review, debug, architecture and codebase research default to `read`; other categories default to `write`. A `needs_input` result blocks the node and its dependents (protocol in section 8.2).

```yaml
spec_version: 1
run_id: auto
policy: { on_failure: continue-independent, max_depth: 2, max_nodes: 20, cost_ceiling_usd: 15, cost_mode: advisory }  # cost_* fields: phase 4; rejected as unsupported before then
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
- Per-node timeout, `max_attempts`, retry with model escalation, then fallback to the next agent. Each retry, escalation and fallback goes back through the Router and writes a route log entry (`escalated` / `fallback`, section 4.4) before the new attempt starts.
- Failure policy per graph: `fail-fast` stops dispatch and cancels active attempts after a terminal failure; `continue-independent` (default) finishes unaffected branches and transitively blocks dependents of unsuccessful nodes. `continue-all` is reserved and rejected in v1: running a dependent without a successful input snapshot would violate the dependency contract. Conflict-resolution failures follow the selected policy rather than unconditionally stopping independent work.
- Cancellation (`orch cancel` or OpenCode abort) sends a kill to the whole process tree of each running worker, marks nodes `cancelled`, and leaves worktrees for inspection.
- States: `pending | running | done | failed | blocked | cancelled`, persisted in `run.json`; `orch resume <run>` reconciles interrupted attempts, then reuses `done` nodes only when their complete `input_key` and stored outputs still match (section 6). Invalidated descendants return to `pending`; independent unchanged nodes can be reused. Outstanding cost reservations survive restart (section 8.5).
- `max_attempts` is a total per-node limit across all agents, escalations, and fallbacks; it does not reset on agent switch or resume. A schema-repair follow-up is limited to one per attempt and is charged to that attempt. Each attempt persists its phase (`agent`, `capture`, `verify`, `publish`) so a timeout or crash during verification cannot be mistaken for success. Before building an attempt's invocation, the orchestrator moves any `result.spec.yaml` left in the node directory to `attempts/NN/` (`NN` the attempt before the one starting; a file already archived there is never overwritten, the newer one gets a numbered name beside it), for every adapter: the parsing order of section 7.3 then reads only the current attempt's output, and earlier results, including a `needs_input` question set, stay inspectable. Node cancellation blocks its descendants; run cancellation also marks pending nodes `cancelled`. A quiescent run with blocked nodes is `blocked`, not successful; final integration is published as complete only when all required nodes are `done`. Partial outputs remain inspectable.
- **`needs_input` protocol.** An agent that needs operator clarification returns `status: needs_input` with structured queries in `open_questions`. The orchestrator (the supervisor from phase 2b, the foreground process in phase 1) sets the node `blocked` (reason `needs_input`), preserves the questions in `result.spec.yaml`, publishes no successful output, and blocks dependents until the operator answers via `orch reply <run> <node> "<answers>"` or the OpenCode `task_input` tool. Answers are persisted to `input_answers.yaml` in the node directory and rendered into the next attempt's prompt as operator input; input submitted through a model tool is labeled host-submitted content unless the host independently attests operator authorship, and is never elevated to trusted operator policy. A `needs_input` attempt still consumes an attempt and its actual duration/cost. Accept answers only for the current `needs_input` question set, using stable question IDs, a question-set revision, and an idempotent submission ID. Persist answers append-only with operator provenance and include their content hash/revision in the effective task input key. Duplicate submissions have no effect; stale revisions are rejected. After recording answers, enqueue only if attempt, execution-time and monetary limits permit; otherwise keep the node `blocked` with the specific exhausted-limit reason and preserve its answers. The operator can explicitly increase the total limit via `orch resume --max-attempts N` (never an implicit reset). Time waiting for operator answers is excluded from cumulative active execution duration. A fresh CLI session is required when answers change the input key; preserve the node's prior worktree edits, but never reuse an old successful output under the new key.
- **`needs_input` in phase 1 (foreground mode, no supervisor or IPC).** This feature is prioritized into the MVP because unattended agents otherwise guess at ambiguous requirements. The adapter prompt tells the agent to stop and return `status: needs_input` with typed questions (`open_questions`, schema in section 5.3) when it cannot resolve a decision from the repository, rather than guessing; free-text questions inside a `done` result remain advisory, so the feature depends on the agent following the result-spec protocol (canary in `orch doctor`). When a node returns `needs_input`:
  1. **Interactive terminal** (stdin and stdout are a TTY and `--non-interactive` is not set): `orch run` prints the questions (id, text, options, default), reads the answers inline, records them (provenance `operator-tty`, a fresh `submission_id`, the current `question_revision`), and starts the next attempt in the same process. Time spent waiting is excluded from the node's active duration. The operator may answer `:abort` to cancel the run (exit code 4) or press Enter on a question with a default to accept it.
  2. **Non-interactive** (CI, pipes, `--non-interactive`, or the operator declines to answer): the run is persisted as `blocked` (reason `needs_input`), the foreground process releases its lock and exits with code 6, and prints the exact `orch reply <run> <node> ...` command. `orch reply` is privileged (CLI confirmation or `--yes`, section 8.6), records the answers append-only with provenance `operator-cli`, and with `--resume` (or a following `orch resume <run>`) re-runs the node in the foreground, taking the interim run lock (section 6). Reply and resume are file-based in this phase: no IPC is involved.
  3. **Shared rules from the protocol above apply unchanged:** the blocked attempt counts against `max_attempts` and cost; duplicate submissions are idempotent and stale revisions are rejected; answers change the `input_key` and force a fresh CLI session while preserving the node's worktree edits; if limits are exhausted the node stays `blocked` with the exhausted-limit reason until `orch resume --max-attempts N`.
  4. **Not in phase 1:** replies routed to a detached supervisor, graph-level blocking of dependents (a phase 1 run has one node), the OpenCode `task_input` tool, and `wait` semantics; they arrive with phase 2b and phase 3 and reuse the same answer files and event types.

### 8.3 Isolation and merging for parallel writers
- Scheduling uses each writer's effective allowed write scope: explicit `diff_scope`, otherwise `owns`, otherwise the entire repository. Two writers may run concurrently only if those scopes are provably disjoint; an unknown glob intersection is treated as overlap. Broader `diff_scope` therefore broadens scheduling reservations even when `owns` is narrower. This prevents undeclared/generated secondary files from bypassing overlap detection.
- At run creation, resolve and pin the base commit. For ordinary clean runs, this is the current target branch HEAD. When `--allow-dirty` is enabled, the orchestrator creates a synthetic base commit (`refs/orch/<run>/base`) whose tree captures the initial tracked and untracked user snapshot and whose parent is the checked-out branch HEAD. Because that snapshot copies untracked, non-ignored files into git objects (and into every node's worktree), the orchestrator first scans the untracked set for sensitive-looking paths (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `*.pfx`, `credentials*`, `*secret*`) and lists matches in the run-start confirmation; the user can proceed, or exclude them with `--exclude <glob>` (repeatable; excluded paths are absent from the snapshot and from every worktree, and are never delivered by `apply`). Refs under `refs/orch/` are never pushed by `orch`, and `orch clean --purge-runs` deletes them; the matches (paths only, never contents) are recorded in the run start event. Every node receives an immutable `input_commit`: the base commit (or synthetic base commit under `--allow-dirty`) for roots, its dependency's `output_commit` for a single dependency, or a deterministic merge of dependency outputs for fan-in. Dependencies are merged in stable topological order with node id as the tie-breaker; ancestor commits are included only once. No node implicitly reads a moving branch tip.
- Each write-capable node gets its own `git worktree` on branch `orch/<run>/<node>` at its input commit. Effective write scopes (including `owns` where applicable) are checked at planning time; overlapping scopes are serialized or reported. Serialization alone does not propagate edits: a later node needing an earlier node's changes must have an explicit dependency. If the repository uses Git submodules or Git LFS, the worktree manager runs `git submodule update --init --recursive` and `git lfs pull` (if installed) upon worktree creation before invoking the adapter.
- **Path length (Windows):** worktrees nested under the repository add roughly 55 characters before any project depth, and tools such as `node_modules` trees exceed the 260-character limit quickly. On Windows `paths.worktree_root: auto` therefore resolves to a short root outside the repository (`%LOCALAPPDATA%\orch\wt\<8-char repo hash>\<run-short>\<node-short>`, where `<run-short>` is the first 8 characters of the run id and `<node-short>` is the node id truncated to 16 characters plus a 4-character hash when truncated); the full ids stay in `run.json`. Worktrees are created with `core.longpaths=true` (set per worktree, never in the user's global git config). `orch doctor` reports Windows `LongPathsEnabled`, creates and removes a path of 300+ characters in the chosen root, and fails early with the remedy (enable long paths, or set a shorter `paths.worktree_root`). On other platforms `auto` is `.agents/worktrees`, and an explicit absolute path may be used anywhere.
- Shared package/build cache contention: concurrent commands (`npm install`, `cargo build`, etc.) across parallel worktrees sharing user cache directories (e.g. `~/.npm`, `~/.cargo`) can race on file locks. The worktree manager isolates local target caches (e.g. `CARGO_TARGET_DIR=.agents/scratch/<node>/target`) or serializes cache-mutating build phases when cache contention risks are declared in `PROJECT.spec.md`.
- Read-only nodes (`read` access: research, review, debug, architecture; section 8.1) get a detached checkout at the same dependency-derived input commit, with `readonly` enforcement. They never run against the main working tree. A reader's `output_commit` equals its `input_commit`, so code snapshots propagate through review/research chains. Result specs and artifacts are captured through the node output channel, outside the repository snapshot.
- **Change capture and commit ownership:** agents are asked to leave edits uncommitted. Once the worker exits, the orchestrator captures the complete delta from `input_commit`, including agent-created commits, staged and unstaged edits, untracked non-ignored files, deletions, renames, binary data, and file modes. Ignored outputs are excluded unless explicitly declared as deliverables. It materializes that delta in a separate candidate checkout, enforces scope, and runs verification there. Agent-reported file lists and commit SHAs are informational only.
- On successful verification of an ordinary write node, the orchestrator creates a commit whose parent is `input_commit` and whose tree is the captured candidate tree, then advances the node branch and records `output_commit`, the diff artifact, and verification evidence before marking the node `done`. No-change nodes reuse the input commit. Verification-generated outputs do not enter the deliverable; changes to tracked candidate files during verification fail the attempt. Failed attempts retain their worktree and artifacts for inspection but publish no successful output commit. Retries may continue those edits within the same node and input snapshot; fallback receives the captured attempt state explicitly.
- Merge nodes are implicit before fan-in. On conflict, create a `resolve-conflict` task with the exact merge inputs and unresolved paths, subject to the same permission, budget, scope, capture, and verification rules as other writers, bounded to `max_attempts: 1`. If automated conflict resolution fails, the fan-in node is marked `failed`; failure policy determines whether independent work continues. Its verified resolution becomes the merge output. Run the graph's integration acceptance commands on every merged snapshot before releasing dependents.
- Completion also merges all terminal outputs into `orch/<run>/integration` and verifies that final tree, even when no explicit fan-in exists. `run.json` and CLI/plugin results expose its branch, commit, and diff from the pinned base. The user's checked-out branch is not advanced automatically. To apply verified deliverables to the active working tree or branch, the user runs `orch apply <run>` (or OpenCode `task_apply`, section 13.1), a privileged command that needs user confirmation (section 8.6). Behaviour:
  - **Git mode:** pin the target repository, branch and current target commit, then prepare a fast-forward or merge with the run's integration commit in a disposable checkout. Conflicts abort preparation without touching the user's checkout; do not start an in-place merge there. Run authorized integration verification on the exact prepared tree, since target-branch edits made after the run can introduce regressions even in a clean textual merge. Existing evidence may be reused only when both tree and verification inputs match. Verification failure rejects application, even with `--force`.
  - Compute the target-to-prepared-tree delta, including both old and new rename paths and directory/file collisions. Compare it with staged, unstaged and untracked user edits; disjoint dirty edits are preserved, while overlap aborts with a path list. For `--allow-dirty` runs, user working-tree edits whose contents and modes match the captured baseline snapshot tree are recognized as pre-existing base state rather than concurrent conflicting edits, allowing safe delivery. Recheck target ref, index identity, file contents and modes immediately before installation. A changed target invalidates preparation and confirmation. `--branch B` creates a previously nonexistent branch pointing at the verified prepared commit and leaves the current checkout/index untouched; it does not silently switch away from dirty edits. For `--allow-dirty` runs, captured user changes are reported explicitly as part of the proposed delivery.
  - **Shadow-git mode (phase 4):** keep the run's original baseline immutable. Prepare a per-path three-way comparison of baseline, current workspace and integration output; include additions, deletions, both rename paths, file types and modes. A path already equal to the desired output is a no-op. Unchanged user paths may be updated, and unrelated user edits are preserved. Conflicting paths abort the entire application before writes by default. Store conflict manifests and incoming contents under a uniquely created `.agents/apply/<operation-id>/conflicts/`, including deletion markers and baseline/current/desired hashes; never overwrite a sibling `<path>.orch-incoming` file. Explicit `--force` authorizes only the listed conflicting replacements in the confirmed plan and retains their before-images; it does not bypass containment, verification, or concurrent-edit checks.
  - **Application transaction (shadow-git file sync, phase 4; git mode in phases 1 to 3 relies on git's own merge plus an expected-ref update, with no journal):** serialize applications to the same canonical target workspace across runs with a separate native target lock (section 6). Before changing any user file, index or branch ref, durably record an operation id, target identity/ref, prepared commit/tree, expected per-path contents/types/modes, before-images, desired outputs, and verification/confirmation identity in a journal under `.agents/apply/`. Write a progress record for each mutation; update refs with expected-old-value checks. Multi-file delivery is not filesystem-atomic. After interruption, reconcile paths equal to before/after images and complete or roll back idempotently; a path matching neither is a concurrent user edit and blocks recovery without overwriting it. Do not admit another application to that target until recovery is resolved. Revalidate ancestor directories and reject traversal through symlinks/junctions or paths outside the target; unsupported file types fail before writes.
  - Outcome is recorded as `apply.finished` keyed by operation id, target identity and delivered tree, not by run id alone. A crash after file installation but before this event is recovered from the journal; reapplying the same run to another branch is a distinct operation. Advance only the workspace delivery ledger after successful sync, never the run's baseline. Application failure has its own status and does not rewrite a previously successful run as failed. Original final-integration merge/verification failure still fails the run.
- **Merge ancestry:** both automatic and resolved merges publish a merge commit with the contributing output commits as parents (or fast-forward when possible). A resolver's ordinary single-parent attempt commit supplies the verified resolution tree only; the supervisor publishes that tree with the original merge parents. Never flatten a fan-in into a single-parent commit, which would lose ancestry and reintroduce resolved conflicts in later fan-ins. Conflict-task scope includes the full combined delta of the merge inputs, with resolution edits restricted to declared unresolved paths unless explicitly widened. Keep immutable per-attempt output refs and merge refs under the `refs/orch/<run>/...` namespace until explicit run purge (`orch clean --purge-runs`) so worktree pruning, retries, and Git garbage collection do not remove reusable snapshots.

### 8.4 Sub-agents requesting work
A result may include `spawn_requests`. The router validates each against `max_depth`, `max_nodes`, remaining budget and permission policy, then adds accepted nodes to the DAG. Rejected requests are logged in `events.jsonl` and returned to the requester's summary. No unbounded recursion.

Requests may add fresh nodes and edges into those nodes only; they cannot change inputs of running or completed nodes. Added nodes undergo the full graph validation (section 8.1: acyclicity and reserved-path checks) and write-scope reservation checks before admission. Conflict resolution is an internal operation with an explicit supported task kind (`resolve-conflict`), not an unknown user taxonomy category, and cannot recursively request another resolver for the same merge.

### 8.5 Cost admission and accounting

- `policy.cost_mode` is `advisory` by default; `cost_ceiling_usd` and node `max_cost_usd` are scheduling thresholds in this mode, not guaranteed billing caps. Node limits cover all attempts, validation-repair calls, escalation, and fallback. Run accounting also includes classifier/planner calls, conflict resolution, and spawned work.
- Pricing basis and settlement: rates (`input_per_m`, `output_per_m`, `cache_read_per_m`, `cache_write_per_m`) are snapshotted with currency, source, and effective date. Adapters normalize provider usage into disjoint uncached-input, cached-read, cache-write, and output token counts. If a provider includes cache reads in total input, subtract them before applying the uncached rate; never charge the same tokens twice. Compute USD as the sum of each disjoint bucket times its per-million rate, divided by 1,000,000: `(uncached_in * input_per_m + cache_read * cache_read_per_m + cache_write * cache_write_per_m + out * output_per_m) / 1,000,000`. Missing rates or ambiguous usage are labeled `estimated` or `unknown`, never exact or zero; a cache rate omitted from `agents.yaml` counts as missing for labeling purposes even though section 3 supplies the `input_per_m` placeholder for the arithmetic, so such costs are `estimated` and never satisfy hard mode. Direct monetary cost takes precedence, with its billing basis recorded (metered API charge versus subscription-equivalent estimate). Unknown pricing cannot qualify for hard mode.
- Before each paid invocation, the scheduler atomically records a reservation under the run lock. Available budget is the ceiling minus settled charges and outstanding reservations. Advisory reservations use a documented estimate; if it does not fit either the run or node remainder, select a cheaper eligible model or mark the node `blocked` with reason `budget_exhausted`. No retry or fallback bypasses admission.
- `cost_mode: hard` is accepted only when every paid invocation has an enforceable upper charge bound from adapter-supported limits and a known pricing basis. Reserve that upper bound. Final-only usage reports, estimates, timeouts, and process cancellation alone cannot establish such a bound. Reject the configuration before execution if any selected invocation cannot satisfy it; runtime fallback must satisfy the same condition. Among the verified CLIs only Claude exposes a spend flag (`--max-budget-usd`, print mode only); whether it is a strict upper bound or is checked between turns is unverified, so until a canary proves a strict bound no adapter qualifies and `hard` is rejected everywhere.
- On completion, settle the reservation using reported usage and its pricing basis, or a labeled estimate in advisory mode; refund the unused reservation. Unknown charges retain their reservation until reconciled. Persist settlement idempotently by attempt id so resume neither loses nor double-counts charges. Interrupted workers must be reconciled or terminated before a replacement attempt is admitted.
- If advisory charges exceed a reservation or reach the ceiling, record the overrun and stop admitting paid work. Workers already running may incur further charges; request cancellation and retain their outstanding reservations until settlement. Status reports show settled cost, reserved cost, estimate/unknown labels, and enforcement mode.

### 8.6 Supervisor lifetime and background execution (phase 2)

Phase 1 has no supervisor: `orch run` executes in the foreground, `wait` semantics do not apply, and `status`/`result`/`routes`/`explain` read the run directory directly. Everything below describes phase 2 and later.

Both front-ends start or attach to a per-run supervisor process implemented by the shared core. It owns scheduling, workers, verification, locks, and durable state; it does not live inside an OpenCode tool invocation. Start it detached with a hidden window on Windows and redirected logs, then wait for an IPC-ready acknowledgement before returning a run id. Persist the supervisor identity and an authenticated local endpoint in the run directory (`ipc.sock` Unix domain socket with 0600 permissions on Unix/macOS; named pipe `\\.\pipe\orch-<run-id>` with an owner-only DACL on Windows, address recorded in `ipc.endpoint`). If the canonical `.agents/runs/<run-id>/ipc.sock` path on Unix exceeds 100 bytes (approaching OS `sun_path` limits of 104–108 bytes), bind the socket under the platform temp directory (`$TMPDIR/orch-<repo-hash>-<run-id>.sock`, permissions 0600) and record the actual path in `ipc.endpoint`. CLI/plugin status, cancellation, replies, and resume use this endpoint; no agent task needs to remain attached to keep execution alive.

**IPC threat model.** Workers run as the same OS user as the supervisor, and at `full` permission they can read any file that user can read, including run-directory tokens and the IPC endpoint itself; file permissions (0600, and especially on Windows) therefore cannot keep a compromised or prompt-injected agent out. The design does not rely on them for protection of privileged operations:
- Commands are split into **observer** (`status`, `result`, `routes`, `explain`, `replay`; authenticated by `ipc.token`) and **privileged** (`apply`, `reply`, `cancel`, `resume`, `clean --purge-runs`, `spawn` approvals, permission changes).
- Use available OS peer identity APIs and reject known worker-tree clients for privileged commands. Peer capabilities differ by platform; some APIs expose credentials without a usable process ID. Treat this as best-effort misuse detection, never proof of user intent or a security boundary against same-user unrestricted workers.
- Privileged commands require operation-scoped confirmation in an interactive CLI, explicit caller `--yes`, or an available host permission flow. Bind confirmation to a prepared operation digest (run/target, action, content, limits, and expected state revision); do not let a model-provided boolean count as host approval. Where no host permission API is available, return an approval-required result and the concrete CLI command. The supervisor cannot prove that `--yes` was typed by a human: an unrestricted same-user process can invoke it too. Confirmation is an operator workflow guard, not isolation from `full` workers. Stronger protection requires a separately trusted broker/identity or an enforced sandbox denying worker access to control state and IPC; v1 does not claim such a boundary at `full`.
- Worker environments never contain the token, endpoint address or run-directory secrets (section 9.4), and `prompt.md`/`stdout.log` are redacted for them.
- Residual risk, documented for users: an agent running at `full` can access user files, IPC, and `.agents/` state. Git recovery covers captured repository edits only and does not bound external damage. Only a verified sandbox profile establishes a restricted boundary; peer checks, confirmation prompts and token omission do not substitute for it.

`wait` controls only the caller's wait for a result. A wait deadline returns the actual persisted state (which may be pending, blocked, or running) without cancelling the run. Closing a CLI or reloading the plugin disconnects the observer. Explicit user session abort cancels runs owned by that session; expiration of a tool wait or client request does not. Persist the originating session/run association so aborts and reconnection can be reconciled after plugin reload. Supervisor death follows section 6 recovery; there is no automatic second owner on a missed heartbeat.

---

## 9. Permissions

**Default: highest permission** for every agent CLI, so tasks do not stall on prompts. Restriction is explicit and opt-in.

### 9.1 Levels

| Level | Meaning |
|---|---|
| `full` (default) | Agent may edit files, run any shell command, use network and tools without approval. |
| `workspace` | Repository writes are confined to the node worktree. Explicit writable exceptions for node output, scratch space, and required CLI state are recorded in the enforcement profile. Other filesystem writes are denied, including from shell subprocesses. Read and network allowances are declared separately. |
| `readonly` | Agent may read and analyze the input snapshot; repository writes and mutating tools are denied. Only declared node-output, scratch, and required CLI-state paths may be writable. |

### 9.2 Where to set it, and precedence
- CLI: `orch run --permissions readonly`, `--agent-permissions codex=workspace,claude=full`.
- `agents.yaml`: `defaults.permissions` and per-agent `permissions`.
- Task spec: `permissions:` per node.
- OpenCode plugin: tool argument `permissions` on `run_agent_task` / `run_task_graph`, and plugin option `permissions` in `opencode.json`.

All explicit restrictions are cumulative: the effective level is the most restrictive of task, CLI/tool argument, applicable agent config, plugin option, and global default (`readonly` < `workspace` < `full`). An omitted value adds no restriction. Source order never widens an explicit restriction. A node marked as read-only by the planner also imposes `readonly`, even when the run default is `full`.

### 9.3 Mapping and enforcement
Adapters implement the mapping in the table in section 7.2. Restricted levels require a concrete enforcement profile identifying the OS, CLI version, sandbox mechanism, writable exceptions, tool restrictions, and read/network policy. Enforcement profiles rely either on CLI-native sandboxing (e.g. Codex `-s workspace-write` backed by bubblewrap on Linux or seatbelt on macOS) or host OS sandbox wrappers managed by `orch` (e.g. `bwrap` on Linux, `sandbox-exec` on macOS; on Windows only filesystem-confining mechanisms qualify, such as AppContainer or a restricted token with a write-restricting DACL, or running the agent in WSL/a container; plain Job Objects limit CPU, memory and process trees but do not restrict file access and do not qualify). Until such a profile is implemented and passes the `orch doctor` canaries, native Windows `workspace` is reported unsupported for every agent that has no verified CLI-native sandbox on Windows, and `readonly` is advertised only when the complete profile prevents repository mutation by all enabled tools and subprocesses. An after-the-fact immutability check detects violations but cannot enforce the level. Early Windows releases may therefore expose only `full` for an adapter; read-access nodes requiring `readonly` fail closed until a qualifying profile exists. Approval flags or a prompt alone do not establish workspace confinement. On platforms where neither CLI-native nor host-managed sandbox mechanisms exist for a given agent, `workspace` is excluded from `probe().levels`, and tasks requesting `workspace` fail closed with an explicit error rather than running unconfined.

`orch doctor` checks each profile in disposable directories, including an allowed write, denied writes outside the worktree (also through a shell subprocess and a symlink), denial of repository writes for `readonly`, and access to declared node-output paths. Unsupported OS/version combinations are excluded from `probe().levels`; flag snapshots alone are insufficient. If a requested level cannot be honored, the router picks an eligible agent or fails a pinned node with a clear error. It never silently widens permission. Worker enforcement covers the agent process tree; the orchestrator's candidate capture and verification use a separately recorded profile, and read-only nodes do not run build/test commands that mutate their checkout.

### 9.4 Safety nets that remain on at `full`
- Worktree and branch per write node preserve captured repository edits for inspection and reversal; they do not undo external effects of unrestricted shell commands. Pre-run `git status` must be clean unless `--allow-dirty` is supplied. `--allow-dirty` snapshot semantics: section 8.3.
- Secrets: the child environment is built from a platform-safe base allowlist plus variables each CLI needs for auth (e.g. `CURSOR_API_KEY`, provider keys); the parent environment is not forwarded wholesale. The base allowlist includes platform-essential variables: on Windows, `SystemRoot`, `SystemDrive`, `WinDir`, `ComSpec`, `PATH`, `PATHEXT`, `TEMP`, `TMP`, `APPDATA`, `LOCALAPPDATA`, `ProgramData`, `ProgramFiles`, `ProgramFiles(x86)`, `USERPROFILE`, `USERNAME`, `HOMEDRIVE`, `HOMEPATH`, `SSH_AUTH_SOCK`, `GIT_SSH`, `GIT_SSH_COMMAND` (omitting these causes Windows socket/runtime APIs, node spawns, `.cmd` shims, OpenSSH agent pipes, npm and git to fail); on Unix/macOS, `PATH`, `HOME`, `USER`, `SHELL`, `TMPDIR`, `LANG`, `LC_ALL`, `TERM`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `SSH_AUTH_SOCK`, `GIT_SSH_COMMAND`. On all platforms the base also passes common toolchain roots when present in the parent environment (`JAVA_HOME`, `GOROOT`, `GOPATH`, `CARGO_HOME`, `RUSTUP_HOME`, `DOTNET_ROOT`), network and trust settings so CLIs work behind corporate proxies and TLS inspection: `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY` (either case), `SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and each CLI's config-home override when set (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME` **[verify name]**). Proxy URLs may contain credentials, so they are passed but never logged. Extra variables can be injected via `env:` in `agents.yaml`; the supervisor's own variables (IPC token, endpoint, run secrets) are never forwarded.
- **Prompt-injection hygiene**: content originating from other agents or from repository files is wrapped and labeled as untrusted data in downstream prompts. The router executes only verification commands resolved from user-authorized project configuration or graph/task specs (`commands`, executable `acceptance`, and `integration_acceptance`). Spawn requests cannot introduce or alter those commands or disable verification; they inherit the authorized verification policy.
- A banner at run start states active permission levels per agent; `events.jsonl` records the level actually used by each node.

---

## 10. Verification

The Verifier runs after every write node and after merges, against the captured candidate tree from section 8.3. It does not trust agent self-reports. Graph-level `commands` and `verify` are inherited by nodes unless explicitly overridden; repository defaults are supplied by the run's resolved project configuration.

1. `verify` defaults to `default` when omitted. In this mode, always run explicit executable `acceptance` entries; if there are none, run every configured build/test/lint command. A `diff_scope` entry is a scope check and does not count as an executable check or suppress defaults. If no executable checks can be resolved for a write node, reject the task before dispatch rather than report an untested change as verified.
2. User-authored `verify: none` with a nonempty `verification_skip_reason` may explicitly skip command checks. Scope checks still apply, and verification is recorded as `skipped` with that reason, never `passed`. Agents and spawn requests cannot add this opt-out. Read-only analysis nodes record command verification as `not_applicable`; they still undergo result validation and repository immutability checks.
3. Scope checks use the complete captured delta, including staged, committed, and untracked deliverables. `owns` is a planning/scheduling constraint used to detect concurrency hazards and serialize parallel writers; `diff_scope` is the Verifier acceptance check constraint. If `diff_scope` is specified, modified files must fall within `diff_scope`. If `diff_scope` is omitted, `diff_scope` defaults to `owns` (if specified) or the entire repository. If both `diff_scope` and `owns` are explicitly provided on a task spec, modified files must satisfy `diff_scope` (allowing `owns` to specify parallel ownership while `diff_scope` explicitly permits generated tests or secondary files). Record command exit codes, optional output patterns, and timeouts. Candidate files must remain unchanged by verification.
4. At merges and final integration, run `integration_acceptance` plus applicable scope checks. The aggregate diff is checked against graph-level scope and the union of allowed paths from contributing writers; individual writer scopes remain checked against their own input commits. If no executable integration acceptance is provided, use graph/repository build/test/lint defaults. Missing executable checks require the same explicit opt-out as write nodes. Integration checks run after all terminal outputs have been combined, even for a single-node write run. A graph containing only read-only analysis nodes with unchanged output trees records integration command verification as `not_applicable`.
5. Record results into `result.spec.yaml.verification`; failed verification publishes no successful output commit and triggers retry with model escalation and budget admission, feeding failing output back via a follow-up prompt, up to `max_attempts`. A node reaches `done` only after result validation, verification (or recorded explicit skip), and output publication succeed.
6. Optional `review` fan-in: a read-only review node (different agent than the author when possible) receives the dependency snapshot and diff from the pinned run base before the graph is marked complete.

---

## 11. Observability

- `events.jsonl` is the canonical, append-only record: one JSON object per line, with flat fields `seq` (starts at 1, no gaps), `ts` (UTC, millisecond precision), `type`, `owner_generation`, optional `hashes` (spec hashes), then the payload fields of the catalog entry. An append is one line flushed to disk before the call returns, so a decision logged before dispatch survives a crash. When the log is opened, a torn trailing line (no newline and not valid JSON) is discarded and a complete trailing line missing only its newline is repaired; an unparsable earlier line is a corruption error, never skipped. Events carry no answer text. Catalog:
  - **Run and supervisor:** `run.started {config_hash, base_commit, allow_dirty, sensitive_paths?}`, `run.finished {state, integration_commit?}`, `supervisor.started {generation}`, `supervisor.recovered {generation, reconciled_nodes}`, `supervisor.lock_failed`.
  - **Node lifecycle:** `node.state {node_id, from, to, reason?}` (every transition, including `blocked` with its reason such as `needs_input`, `budget_exhausted`, `dependency_failed`), `attempt.phase {node_id, attempt, phase}` (`agent|capture|verify|publish`, section 8.2), `node.started`, `node.finished`, `node.reused|invalidated`.
  - **Routing:** `node.routed`, `route.log.write_failed`.
  - **Input:** `input.requested {node_id, question_revision}`, `input.recorded {node_id, question_revision, submission_id, provenance}` (answer content lives in `input_answers.yaml`, only its hash is in the event), `input.rejected {reason}`.
  - **Verification, merge, delivery:** `verify.result`, `merge.result`, `integration.published`, `apply.started`, `apply.progress {op_id, step}`, `apply.finished`, `apply.recovered {op_id, action}`.
  - **Budget, spawn, other:** `budget.*`, `spawn.*`, `permission.denied {node_id, requested, effective}`, `warning`.
  - Detailed fields for the main events:
  `run.started`, `node.routed {node_id, route_seq, decision}` (canonical routing entry; route logs are projections, section 4.4), `route.log.write_failed`, `node.started {permissions, enforcement_profile, input_commit, input_key, cmd}`, `node.finished {status, output_commit, exit, duration, usage}`, `node.reused|invalidated`, `verify.result`, `merge.result`, `integration.published`, `apply.started {op_id, target, commit}`, `apply.finished {op_id, status, error?}`, `budget.reserved|settled|exhausted|overrun`, `spawn.accepted|rejected`, `warning`, with timestamps and spec hashes.
- `orch status <run>` and the OpenCode `task_status` tool render a node table (state, agent/model, duration, cost) from `run.json`.
- `orch replay <run>` re-renders the timeline; `orch explain <run> <node>` shows the routing decision and which rules matched (from the route log entries); `orch routes` lists the route log (section 14).
- Cost/usage is taken from CLI JSON output where available (Claude, Gemini stats), otherwise estimated and flagged as estimated or unknown. Status exposes settled and reserved costs and `advisory|hard` mode; advisory ceilings are never presented as guaranteed billing caps (section 8.5).

---

## 12. Alternatives considered

| Option | Why not (for v1) |
|---|---|
| MCP server exposing each CLI to one lead agent | Delegation becomes non-deterministic, no DAG scheduling, hard to enforce permissions and verification. Kept as a possible third front-end later (core is reusable). |
| A2A / Zed Agent Client Protocol transports | Local CLIs already have headless modes; protocol layers add setup without benefit now. The adapter interface is protocol-neutral so an ACP/A2A adapter can be added. (Note: Zed's ACP, editor↔agent, is distinct from IBM's Agent Communication Protocol.) |
| LangGraph / agent frameworks | Heavy dependency for what is subprocess supervision plus files. |
| Single shared transcript between agents | Large, agent-specific, leaks injection; specs with summaries and references are smaller and auditable. |
| TypeScript core (Node/Bun) shared with the plugin | Needs a native lock binding per OS/architecture/runtime, a Node or Bun runtime matrix, and tree-kill workarounds; the plugin only needs a thin client, so sharing code buys little. |
| Rust or Go core (single binary) | Better distribution and compile-time state machines, but slower to build; the workload is subprocess-bound, so speed is not a factor. Revisit if a zero-dependency install becomes a requirement (a PyInstaller build is the first step). |

---

## 13. OpenCode integration (phase 3; surface 2 phase 5)

Findings from the OpenCode docs: plugins are TS/JS modules (`.opencode/plugins/`, `~/.config/opencode/plugins/`, or npm via the `plugin` array in `opencode.json`) exporting `async ({ project, client, $, directory, worktree }) => hooks`. Hooks include `tool` (custom tools with `tool()` and zod from `@opencode-ai/plugin`; tool context has `agent, sessionID, messageID, directory, worktree`), `tool.execute.before/after`, `permission.asked/replied`, `session.*` events, `shell.env`. Agents are defined in `opencode.json` or `.opencode/agents/*.md` with `mode: primary|subagent|all`, `model`, `permission` (including `permission.task`), `prompt`, `steps`. The SDK offers `client.session.create/prompt/children/abort`, event subscription and structured output (`format: json_schema` → `info.structured_output`). Custom model providers are configured through `provider.<id>` with `@ai-sdk/openai-compatible` and `options.baseURL`; the docs do not show plugins registering providers.

### 13.1 Surface 1: router tools (primary, v1)

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

### 13.2 Surface 2: provider gateway (optional, v2)

To select an external CLI as a *model* inside OpenCode (`/models`), the plugin starts a local OpenAI-compatible server (`/v1/chat/completions`, `/v1/models`) on a loopback port with a random bearer token. Model ids: `router/auto` (route by category), `router/claude-deep`, `router/codex`, etc. Each request becomes a single-node task run, and the final text is returned (streamed as SSE chunks of the CLI's progress/summary).

Configuration: a `provider.router` entry (`npm: @ai-sdk/openai-compatible`, `options.baseURL`, `options.apiKey`, `models`) that the plugin either injects (if the plugin API exposes a `config` hook; **[verify]**, present in plugin source types but not in the docs) or prints as a snippet via `orch opencode-config`.

Limitations, documented for users: the CLI runs its own tool loop, so OpenCode tool-calls are not passed through; no token-level streaming fidelity; context from OpenCode's conversation is flattened into the prompt. Surface 1 is the recommended path for programming workflows; the gateway is for "use that CLI as my model in this session".

### 13.3 Packaging and install
- npm package `@agents-coordinator/opencode-plugin` listed in `opencode.json` `plugin`, or a local file in `.opencode/plugins/`. It requires the `orch` executable (the Python package, installed with `uv tool install` or `pipx`) and checks its version at load.
- The plugin is the only Node code in the project; the core spawns CLIs through Python's `subprocess`/`asyncio`.
- Version compatibility: plugin declares the `@opencode-ai/plugin` version range it was tested with; `orch doctor` checks it.

---

## 14. `orch` CLI

```
orch run "<request>" [--agent A] [--category C] [--permissions full|workspace|readonly]
                     [--agent-permissions a=level,...] [--model-tier fast|balanced|deep]
                     [--concurrency N] [--dry-run] [--allow-dirty] [--exclude GLOB]... [--non-interactive] [--json]
orch plan  "<request>"            # produce a graph.yaml without executing
orch graph run graph.yaml         # execute a graph
orch status [run]   orch result <run> <node> [field]
orch resume <run> [--raise-budget USD] [--max-attempts N] [--node ID]
                                  # resumes interrupted or blocked runs; --raise-budget unblocks nodes blocked with
                                  # reason budget_exhausted; --max-attempts explicitly raises the total attempt cap
orch cancel <run> [node]
orch reply <run> <node> "<answer>" [--answers-file F] [--revision N]
                                  # answer questions and unblock a needs_input node (defaults to current revision if omitted); --resume re-runs it.
                                  # Phase 1: file-based, foreground resume; phase 2b: routed through supervisor IPC (section 8.2)
orch apply <run> [--branch B] [--force] [--yes]
                                  # privileged; behaviour, flags and confirmation: sections 8.3 and 8.6
orch explain <run> <node>         # routing decision and matched rules
orch routes [run] [--tail N] [--node ID] [--agent A] [--rule ID] [--event E] [--json]
                                  # route log (section 4.4); without <run>, reads all runs under .agents/runs
orch agents                       # list agents, availability, versions, enforceable levels
orch doctor                       # probes CLIs, canary runs (incl. session-id extraction), config validation, git exclude block,
                                  # Windows long-path/worktree-root check, pricing-rate warnings, opencode compat
orch clean [--older-than <dur>] [--purge-runs] # prune inactive worktrees/scratch; purge explicitly removes run records and refs
orch opencode-config              # print provider/plugin config snippet
```

**`orch doctor`** runs a registry of checks with stable ids (`python`, `git`, `config`, `agent.<name>.cli`, plus those added by later sections). Each check reports `pass`, `warn`, `fail`, `unsupported` (not available on this machine or platform, with the reason) or `skipped` (a prerequisite failed). Checks that call a model are marked `paid`: they run only with `--paid`, or when named with `--check <id>`, so the default run costs nothing. A check never aborts the run: an exception or timeout is reported as `fail`. The text report has one line per check followed by its remedy; `--json` emits the full report. `doctor` exits `0` when no check failed (warnings and unsupported items are not failures) and `1` otherwise.

Exit codes: `0` all required nodes done and final integration complete (or application succeeded); `1` execution/store failure (or application dirty-overlap abort); `2` invalid input/config; `3` no eligible agent; `4` cancelled; `5` verification failed (including post-merge application verification); `6` blocked/needs input or exhausted budget; `7` approval required: a privileged operation (`apply`, `reply`, `cancel`, `resume --raise-budget`) was prepared but not confirmed, because no interactive prompt or host permission flow was available and `--yes` was not given. Exit `7` changes no state, and its output prints the exact command to confirm it. For mixed outcomes choose cancellation, then verification failure, execution/store failure, no eligible agent, blocked, and approval required in that order. The `task_apply` / `task_input` tools return the same condition as `status: "approval_required"`. `--json` includes run state and per-node reasons so mixed failures remain inspectable.

---

## 15. Tech choice and layout

Python 3.11 or newer for the core and the CLI; TypeScript only for the OpenCode plugin. Libraries: `pydantic` v2 for specs (exported to JSON Schema), `PyYAML` or `ruamel.yaml` for parsing, `asyncio` with `subprocess` for process supervision, and direct `git` invocations for worktrees. Packaging with `uv` (`pyproject.toml`, console script `orch`); tests with `pytest`, lint and format with `ruff`, types with `pyright` in strict mode (the state machines use `Literal` unions and exhaustive `match` so that the checker stands in for compile-time exhaustiveness). Windows process-tree kill uses a Job Object through `ctypes`; Unix uses a new session and `killpg`.

From phase 2 the platform layer additionally needs lifetime ownership and target locks (section 6). They use only the standard library and `ctypes` (`fcntl.flock` on Unix, `LockFileEx` on Windows), so there is no native binding to build or package. Support is capability-tested on the actual OS and filesystem rather than assumed: if the lock semantics are unavailable, `doctor` reports the unsupported combination and execution is disabled. Lock support is a **phase 2 prerequisite** (it gates the detached supervisor, section 8.6), not an MVP prerequisite: phase 1 uses the interim exclusive-create lock described in section 6. The phase-0 spike (executable as `tests/test_locks.py` and `tests/test_ipc_spike.py`) settled the lock and IPC mechanisms and fixed the supported matrix below. A platform outside the matrix keeps the foreground phase 1 mode until it is added.

| Platform (verified) | Locks | IPC |
|---|---|---|
| Windows 11 10.0.26100, Python 3.11, NTFS | `LockFileEx` through `ctypes`. Released immediately when the holder is killed. Not inherited by workers in any `subprocess` mode. Exactly one winner in an 8-way cold start. A held lock file cannot be deleted. | Named pipe only (this Python has no `AF_UNIX` on Windows). asyncio's default pipe grants read to `Everyone` and `Anonymous`, so the pipe is created with an owner-only DACL, `PIPE_REJECT_REMOTE_CLIENTS` and `FILE_FLAG_FIRST_PIPE_INSTANCE` by subclassing asyncio's `PipeServer` (private API: pin Python minor versions and test on each). The peer PID comes from `GetNamedPipeClientProcessId`. |
| Linux (WSL2 kernel 6.18), Python 3.12, ext4, tmpfs and 9p | `fcntl.flock`. Same results on all three filesystems. | `AF_UNIX` stream socket, mode `0600`, peer pid and uid from `SO_PEERCRED`. `sun_path` is about 108 bytes, so deep run directories use the hashed short-path fallback of section 6. |
| macOS, Python 3.11 or newer | Not verified (best effort): `flock` expected to behave as on Linux. | Not verified: peer credentials would use `LOCAL_PEERCRED`; `sun_path` limit 104. |

Two rules follow from the spike. Workers are started by exec only (`subprocess`), never by a bare `os.fork()`, because a forked child shares the open file description and keeps a Unix `flock` alive after its owner dies. Lock files are never unlinked by `orch clean`: Unix allows deleting a held lock file, after which two contenders could lock different inodes. Network filesystems (NFS, SMB) are not verified; `doctor` probes the actual filesystem before execution (task 2.12).

```
pyproject.toml            uv project; console script: orch
src/orch/
  core/     registry.py classifier.py planner.py router.py model_router.py route_log.py
            scheduler.py worker.py worktree.py specs.py store.py verify.py events.py
            platform/{locks.py,proc.py,ipc.py}
  adapters/ base.py fake.py claude.py codex.py gemini.py cursor.py
  cli/      main.py commands/*.py
plugin/                   TypeScript, the only Node code: src/{index.ts,tools.ts,agents.ts,gateway.ts}
schemas/*.json  examples/  docs/design.md  tests/
```

Single-source specs: pydantic models in `orch.core.specs` generate `schemas/*.json` through a build step (including `schemas/route-entry.json`), used both for runtime validation and for the CLIs' `--json-schema` (inline JSON string) / `--output-schema` (file path). The plugin derives its tool argument schemas from the generated files, so there is no second definition.

---

## 16. Roadmap

The MVP is cut as a thin vertical slice that proves the core loop (route, run one CLI in an isolated worktree, capture, verify, deliver) before any concurrency or background machinery is built. Each phase ends with a usable tool.

| Phase | Name | Deliverable | Explicitly not included |
|---|---|---|---|
| 0 | Foundations | Spec schemas (pydantic to JSON Schema), `agents.yaml` loader with merge/validation, run store and `events.jsonl`, `fake` adapter, `orch doctor` basics, repository-local exclude block. Spikes that settle open design questions: the lock and named-pipe behaviour matrix (section 15) and real-CLI behaviour canaries (section 7). | Any real agent run |
| 1 | **MVP: foreground single node** | `orch run` (foreground, one node, one CLI process): rule classifier, router with `routes.rules`, ModelRouter, route log (`routes.jsonl`, `route.log`) and `orch routes`/`explain`; Claude and Codex adapters; worktree capture, verified output commit, final integration branch, default Verifier; permission `full` by default plus `readonly`/`workspace` only where a verified profile passes `doctor` (Windows: `full` only for adapters without one); environment allowlists; interim exclusive-create run lock (section 6); `orch status/result/agents/doctor/clean`; git-mode `orch apply` (prepared-tree verification, overlap check, expected-ref update, `--branch`; no journal). Non-git directories are refused with `git init` instructions. **`needs_input` in foreground mode** (typed questions, inline terminal answers, or `orch reply` plus `orch resume` for non-interactive runs; section 8.2). | Task graphs, concurrency, detached supervisor, IPC, native lock, IPC-routed replies to a background run, shadow-git, OpenCode plugin, Gemini/Cursor, cost ceilings, `spawn_requests` |
| 2 | Graphs and supervisor | **2a (foreground graphs):** scheduler and task graph, dependency snapshots and merge, conflict resolution node, fallback/escalation, input-key reuse and invalidation, `orch graph/plan/resume/cancel` (foreground). **2b (background):** detached per-run supervisor, native lifetime lock (`flock`/`LockFileEx`, validated by the phase-0 spike), authenticated IPC with observer/privileged split, crash recovery and attempt reconciliation, background `needs_input` (replies routed through IPC, dependents of a blocked node held, `wait` semantics). 2b ships only on platforms where the lock spike passes. | OpenCode plugin, shadow-git, cost ceilings |
| 3 | OpenCode plugin | Router tools (`route_task`, `run_agent_task`, `run_task_graph`, `route_log`, `task_*`), companion agent/commands, event mirroring, host-approval flow for privileged tools. Depends on 2b. | Provider gateway |
| 4 | More agents and delivery modes | Gemini and Cursor adapters, `spawn_requests`, cost reservations/accounting (advisory mode; hard mode only if a bound is proven), shadow-git mode with journaled workspace sync and conflict manifests, `replay`. | |
| 5 | Gateway and planning | Provider gateway (surface 2), optional LLM classifier/planner. | |

**Command availability by phase** (the CLI rejects later commands with a clear "available from phase N" error):

| Phase 1 | Phase 2 | Phase 3 | Phase 4+ |
|---|---|---|---|
| `run`, `status`, `result`, `apply`, `reply`, `resume` (single node), `explain`, `routes`, `agents`, `doctor`, `clean` | `plan`, `graph run`, `cancel`, `resume` for graphs, `reply` routed through IPC | `opencode-config` and the plugin tools | `replay`, `--raise-budget`, shadow-git `apply`, cost features |

### Testing strategy

Tests are organized by area; the phase in brackets says when each area must be green. Adapters and the Verifier are tested against the `fake` adapter and recorded CLI outputs first; live CLIs are optional (`ORCH_LIVE=1`).

- **Config and routing [1]:** built-in, user, project merge order; recursive maps; keyed rule merging by `id` (`enabled: false`, `priority`, stable order within a priority); primitive array replacement; retained `false`/empty values; invalid nulls and anonymous/duplicate ids; rule predicate conjunction/disjunction and comparison operators; higher-priority project exceptions; exhausted and busy preference lists; pinned-agent queueing; `risk` derivation and its complexity bump; `language` resolution; a rule using `cost_ceiling` is rejected before phase 4; escalation defaults (agent without `escalate_on_failure`, single-tier agent, top-tier ceiling).
- **Route log and events [1]:** the full routing entry is durable in `events.jsonl` before dispatch (kill the worker immediately and the entry still exists); exactly one entry per attempt, escalation and fallback with correct `event`, `from`, `agent_rule`, `model_rule`; implicit rule ids when no rule matches; `previewed` entries have `executed: false`; `rerouted` on resume with changed inputs; title truncation, secret redaction and `include_goal: false` across canonical events and both projections; a projection write failure warns without failing the run and missing projections are rebuilt from events idempotently by `seq`; `route.log` lines match `routes.jsonl`; `orch routes` filters across runs; catalog completeness (node state transitions, attempt phases, apply steps, no answer text).
- **Adapters [1, 4]:** `buildInvocation` snapshot-tested against the flag table; `parseResult` tested on recorded outputs (golden files); `--bare` omitted for subscription logins; session-id canary per adapter with fresh-session fallback; Windows `.cmd` shim spawning and quoting canary; environment allowlist survival (Windows `SystemRoot`/`PATHEXT`/`APPDATA`, Unix `PATH`/`HOME`/`XDG_*`, proxy/CA variables, toolchain roots, SSH variables) with proxy URLs redacted from logs and no supervisor secrets in worker environments.
- **Worktrees, capture and verification [1, 2a]:** worktree isolation; uncommitted, staged, untracked, deleted, renamed, binary, symlink and agent-committed changes survive capture and final integration; failed verification publishes no commit; verification cannot alter the published tree; the user's checkout is unchanged; omitted `verify` selects defaults, scope-only acceptance still runs defaults, missing executable checks are rejected, explicit skips carry a reason, `owns` vs `diff_scope` boundaries; reserved runtime paths (`.agents/runs/`, `.git/**`) are rejected in specs while `.agents/agents.yaml` edits are accepted without changing the running config; the local exclude block exists exactly once, `git status` and `git add -A` show no runtime state and `.gitignore` is untouched; runtime state is excluded from baselines, dirty checks and capture.
- **Permissions [1]:** most-restrictive-wins property tests; disposable canaries for allowed paths, denied external writes through subprocesses and symlinks, read-only repository immutability, declared output exceptions; a CLI plan mode that can mutate through an enabled tool fails the `readonly` profile; unsupported profiles fail closed (Windows `workspace`/`readonly` reported unsupported without a qualifying profile); no test treats `--yes` or peer credentials as proof of human origin.
- **Delivery, `orch apply` [1 git mode, 4 shadow-git]:** a textually clean merge that fails tests never touches the checkout; changed target refs invalidate approval; disjoint dirty edits are preserved and overlaps abort with a path list; `--allow-dirty` baseline-aware overlap recognition (pre-existing base edits are not conflicts); `--branch` leaves a dirty checkout untouched; sensitive-path scan and `--exclude`; reapplying to another target is a separate operation; exit code `7` changes no state and prints the confirming command. Phase 4 adds shadow-git three-way sync (adds, deletes, renames, binaries, symlinks, modes), unique conflict manifests, crash injection before and after every mutation and before `apply.finished`, journal recovery, and concurrent user edits blocking recovery.
- **Ownership and recovery [1 interim lock, 2b native]:** phase 1: exclusive-create lock, live owner refused, dead owner detected by PID plus start identity, an `interrupted` run re-run. Phase 2b: simultaneous cold starts and stale-socket recovery under the native lock; stale heartbeat while the owner lives; PID reuse; unsupported platform or filesystem fails before writes; descriptors not inherited by workers; purge cannot replace lock objects; owner death during capture/verify/publish; stale attempt results rejected; supervisor survives front-end disconnect; the IPC observer token cannot run privileged commands; privileged commands from a worker tree are rejected (best effort); operation-digest mismatch and missing host approval are rejected as workflow cases.
- **`needs_input`, foreground [1]:** the adapter prompt and result schema produce typed `open_questions`; a TTY run prompts inline, records answers with provenance `operator-tty`, and starts a fresh-session attempt with the node worktree preserved; a defaulted question accepts Enter; `:abort` exits with code 4; non-interactive runs persist `blocked`, release the lock, exit 6 and print the `orch reply` command; `orch reply` rejects stale revisions and duplicate submission ids idempotently, requires confirmation or `--yes`, and `--resume` re-runs the node under the interim lock; the blocked attempt counts against `max_attempts` and cost; waiting time is excluded from active duration; exhausted limits keep the node blocked until `orch resume --max-attempts N`; a free-text question in a `done` result stays advisory; answer content never appears in `events.jsonl`; the `doctor` canary checks that Claude and Codex return `needs_input` for a deliberately ambiguous task.
- **Graphs, resume and input [2]:** ordering, concurrency caps, failure policies and cancellation with a fake adapter; invalid/cyclic graph mutations; total retry limits across fallback and resume; blocked exit codes; overlap tests with narrow `owns`, broader `diff_scope` and missing scope declarations; resolved-diamond-then-fan-in merge ancestry and snapshots preserved after pruning; conflict node bounded to one attempt; snapshot/resume (a review after a writer sees its dependency output; diamonds preserve shared ancestry; disconnected terminal writers appear in final integration; changing an upstream commit, result, artifact, instruction, config or verifier version invalidates descendants while independent nodes stay reusable; missing outputs prevent reuse); `needs_input` in graphs and background runs (the shared rules above also hold): last-attempt input preserves answers and stays blocked until an explicit limit increase, duplicate/stale revisions handled deterministically, answers change the input key and force a fresh session, waiting time excluded from execution duration.
- **Budgets and pricing [4]:** token-to-USD conversion with disjoint buckets and no double charging; omitted cache rates labeled `estimated`, disqualifying hard mode, with the `doctor` warning; estimation fallbacks; concurrent reservations cannot oversubscribe; retries, fallback and planner calls are charged; settlement and restart are idempotent; unknown charges retain reservations; advisory overruns stop admission; hard mode rejects adapters without enforceable bounds.
- **Windows [1]:** short worktree root outside the repo, a 300-character path round-trips, truncated node ids stay unique and map back through `run.json`, `doctor` fails with a remedy when long paths are unavailable.
- **OpenCode plugin [3]:** tool handlers against a mocked `client`, including `wait: false` non-blocking return, wait expiry returning the actual state without cancelling, explicit session abort cancelling, and `approval_required` results; one manual end-to-end in OpenCode.

---

## 17. Risks and open questions

| Risk / question | Mitigation |
|---|---|
| CLI flags change between versions | Adapters are small and snapshot-tested; `orch doctor` canary runs; version ranges recorded in the registry. |
| Structured-output flags differ per CLI and change between versions | Verified against Claude 2.1.308 (inline schema without `$schema`, result in `structured_output`) and Codex 0.163.0 (strict-form schema only, section 5.4); recorded outputs are golden tests (`tests/fixtures/recorded/`); `orch doctor` canary checks the output is schema-valid, otherwise the adapter falls back to a prompt-injected schema. |
| Windows `.cmd` shims (Cursor `agent`) and quoting | `resolveCommand()` per adapter, prompts via stdin/file, quoting canary in `orch doctor` (section 7.2). |
| Gemini CLI availability depends on the user's plan (replaced by Antigravity CLI for unpaid/Google One users, 2026-06-18) | Gemini adapter is optional; `probe()` reports unavailable; routing falls back to other agents. |
| Codex `exec` approval/sandbox behaviour inconsistently documented | Verify with a canary at probe time and record the observed behaviour; do not rely on docs alone. |
| Default `full` permission raises blast radius | Worktree per writer, clean-tree check, env allowlist, banner and event record; restriction flag is one argument. |
| Parallel edits conflict | Effective allowed write scopes, conservative serialization on overlap, bounded conflict resolution preserving merge parents, integration verification. |
| Worktree disk, inode and git-metadata accumulation | Intermediate worktrees are removed with `git worktree remove` and `prune` after verified merge, failed ones are kept for inspection, `orch clean` prunes the rest (section 6). |
| Parallel build/package cache lock contention | Worktree manager isolates build target paths (e.g. `CARGO_TARGET_DIR`) or serializes cache-mutating phases when flagged. |
| Agent claims success falsely | Verifier defaults to executable checks; complete delta capture and orchestrator-owned commits; explicit skips are labeled. |
| Stale review or resumed result | Pin dependency snapshots for all nodes; hash complete inputs and validate stored outputs before reuse; invalidate descendants. |
| Approval settings mistaken for isolation; restricted profiles unavailable (notably native Windows) | Only verified enforcement profiles qualify; unsupported restricted levels fail closed; approval modes and post-run immutability checks do not count (section 9.3). |
| Cost runaway, or costs that look exact | Phase 4 reservations and cumulative accounting; advisory limits disclose overshoot, hard mode needs enforceable bounds, defaulted cache rates are `estimated` and never satisfy hard mode; earlier phases reject configured monetary ceilings (section 8.5). |
| OpenCode tool execution timeouts | Default `wait: false` and a capped `wait_timeout_s`; poll `task_status` (sections 8.6, 13.1). |
| Compromised or injected agent abusing supervisor IPC or the user's branch | Same-user `full` workers are not isolated by prompts, tokens, peer checks or `--yes`; operation-scoped confirmation guards normal operator workflow, while stronger isolation needs a verified sandbox or separate broker (section 8.6). |
| Run ownership: phase 1 interim lock, phase 2 native lifetime lock | Phase 1 foreground exclusive-create lock with PID plus start identity (PID-reuse window and no attach accepted for one user-driven run); phase 2 native lock through `fcntl.flock`/`LockFileEx` with the platform matrix fixed by the phase-0 spike; platforms that fail the spike keep the foreground mode (sections 6, 15, 16). |
| MVP scope creep | The MVP is a foreground single-node slice with a fixed "not included" list; later features are rejected with an "available from phase N" error rather than partially implemented (section 16). |
| Claude `--bare` breaks subscription (OAuth) login | `probe()` records the auth mode; `--bare` only with API-key auth (section 7.2). |
| Delivery to the user's branch: dirty trees, `--allow-dirty`, unapplied results | Target-specific verification, preserved disjoint dirty edits, journaled recovery (phase 4 shadow-git), unique conflict manifests, `orch apply` and `task_apply`, exit code `7` prints the confirming command (sections 8.3, 14). |
| Environment allowlist breaks child processes or corporate networks | Platform essentials, proxy/CA variables and CLI config homes are in the base allowlist (section 9.4). |
| Runtime state pollutes the user's `git status` or gets committed | Marked block in the repository-local exclude file (`info/exclude`, never `.gitignore`), orchestrator path filters, `doctor` check (section 6). |
| Windows path-length failures in nested worktrees | Short worktree root outside the repo on Windows, per-worktree `core.longpaths`, `doctor` long-path canary (section 8.3). |
| `needs_input` depends on the agent following the result-spec protocol | Prompt and typed schema instruct agents to stop and ask; the `doctor` canary checks an ambiguous task yields `needs_input` (in the phase-0 spike both Claude and Codex returned typed questions on the first try, a single run each, so reliability is not yet measured); free-text questions in `done` results remain advisory, so a non-compliant agent degrades to guessing rather than blocking (section 8.2). |
| Session ids: Gemini has no headless resume | Claude, Codex and Cursor confirmed; per-adapter canary; fresh-session fallback with captured state (sections 6, 7.3). |
| `--allow-dirty` snapshot captures sensitive untracked files | Sensitive-path scan and confirmation, `--exclude`, refs never pushed and purged with the run (section 8.3). |
| Route log leaks goal text, or loses decisions on write failure | Redaction and `include_goal: false`; the decision is durable in `events.jsonl` before dispatch and projections are rebuilt (section 4.4). |
| Provider gateway fidelity | Documented as lossy; surface 1 recommended; gateway behind a flag. |
| OpenCode plugin API drift (e.g. `config` hook, subagent session creation) | Isolate OpenCode-specific code in `plugin/`; test against a pinned version range. |
| Open: which task graph planner is default outside OpenCode? | Proposal: template pipelines (bugfix, feature, refactor, upgrade) plus user-supplied graphs in v1; LLM planning in phase 5. |
| Open: do users want a TUI/dashboard? | Out of scope for v1; `status --json` is the integration point. |

---

## 18. References

- Claude Code headless: https://code.claude.com/docs/en/headless
- Claude Code permissions and sandboxing: https://code.claude.com/docs/en/permissions#how-permissions-interact-with-sandboxing
- Codex CLI reference: https://developers.openai.com/codex/cli/reference
- Gemini CLI headless: https://google-gemini.github.io/gemini-cli/docs/cli/headless.html
- Cursor CLI parameters: https://cursor.com/docs/cli/reference/parameters
- OpenCode plugins: https://opencode.ai/docs/plugins/
- OpenCode agents: https://opencode.ai/docs/agents/
- OpenCode providers: https://opencode.ai/docs/providers/
- OpenCode SDK: https://opencode.ai/docs/sdk/
- OpenCode custom tools: https://opencode.ai/docs/custom-tools/
- Agent protocol survey: https://arxiv.org/abs/2505.02279
- Native ownership locks: [Linux `flock`](https://man7.org/linux/man-pages/man2/flock.2.html), [Windows `LockFileEx`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex).
- IPC transport lifecycle: [Python asyncio Unix sockets and Windows named pipes](https://docs.python.org/3/library/asyncio-eventloop.html#creating-network-servers) (transport discovery is separate from native ownership locking).
