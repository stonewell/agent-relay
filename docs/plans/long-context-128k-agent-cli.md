# Development Plan: 128k Context Reliability for Agent CLIs in `relay serve`

**Status:** Steps 2-4 implemented and covered by unit tests (full suite 677 passed, 2 skipped; `ruff` clean). Open: Step 0 (verify codex, agy, cursor against real CLIs) and Step 1 (`agy` stdin, blocked on `-p -` verification). Claude verified against the real CLI (§3). The state in §1 is the state *before* this work.
**Area:** `src/relay/adapters/*_completion.py`, `src/relay/core/platform/proc.py`, `src/relay/gateway/runtime.py`, `src/relay/core/builtin_agents.yaml`
**Related:** [`agy-128k-context-support.md`](agy-128k-context-support.md) (stdin delivery for `agy`; this plan covers the other layers and the verification gates).
**Target:** Every agent CLI served by `relay serve` handles at least a 128k-token context correctly for multi-file editing, exploring, large-context thinking and planning, and a hung CLI is detected in a bounded, size-appropriate time instead of after 30 minutes.

---

## 1. Failure points

128k fails at four separate layers. Fixing prompt delivery alone is not enough.

| Layer | State before this plan | Gap |
| :--- | :--- | :--- |
| 1. Prompt delivery | claude, codex, cursor pass the prompt on `stdin` (`claude_completion.py:267`, `codex_completion.py:376`, `cursor_completion.py:322`). `agy` passes it in `argv` and rejects > 30 KB (Windows) / 120 KB (Linux) (`PROMPT_LIMIT_BYTES`). | `agy` cannot take 128k. |
| 2. Request / output limits | `max_request_bytes` and `max_output_bytes` are 8 MB (`runtime.py:46-47`). | None. |
| 3. Liveness | Only deadline for most backends is `timeout_s: 1800` (`runtime.py:45`, `builtin_agents.yaml`). `agy` has a flat 300 s idle kill, in live (stream) mode only (`agy_completion.py:703-730`). `proc.py` samples `last_output_at` and logs "cli silent" but never acts (`proc.py:653-689`). | A hung claude/codex/cursor runs 30 min; one idle value is wrong for both tiny and huge prompts; non-live `agy` has no idle detection. |
| 4. Agentic correctness | Editing and exploring happen in the client via `tool_calls`; each request re-sends the full transcript. | 128k prefill cost recurs on every tool round-trip. |

## 2. Token/byte conversion

Do not assume a fixed bytes-per-token ratio. Measured with the Claude adapter on synthetic random-letter text: 102,606 bytes = 74,815 tokens (about 1.4 bytes/token). Ordinary English is closer to 4 bytes/token. So 128k tokens is roughly 175 KB (worst case) to 512 KB (prose). Size-based thresholds in this plan must therefore be treated as estimates, and the gateway's advertised `context_length` (`discovery.py`, `models.py`) should stay the source of truth.

## 3. Verification so far

Probe: the real relay adapters (`ClaudeCompletionAdapter`, `CodexCompletionAdapter`) driven with a prompt containing a code word at the start, filler, and a code word at the end, asking for both. Run on Windows with `claude` 2.1.318, `codex-cli` 0.166.0, `agy` 1.5.0.

| Backend | Result |
| :--- | :--- |
| **claude** | Passed, before and after the Step 3 change. Pre-change (`--output-format json`): 102,606 B (74,815 tokens) correct recall in 5.3 s; 573,648 B (406,834 tokens) correct recall in 20.9 s. Post-change, adapter `complete()` with the internal `stream-json --verbose --include-partial-messages` path (command confirmed by spying on `run_process`: prompt on stdin, limits 150 s first-output / 300 s idle): text turn 102 KB (74,812 tokens) 4.6 s, 573 KB (406,829 tokens) 16.1 s; **`output_schema` turn** (`--json-schema` combined with stream-json) 102 KB 4.9 s and 573 KB (814,713 tokens) 13.2 s. Every run returned the correct start and end code words (`MANGO-17`, `PAPAYA-42`) with usage populated. Watchdog check against the real CLI: `Agent(first_output_timeout_s=0.05)` returned 504 `timeout` ("Claude CLI produced no output for 0.1s (first-output limit); prompt 6 bytes") in 0.14 s and left no adapter-spawned `claude.exe` behind. |
| **codex** | **Not verified.** First turn failed with "You hit your spend cap set by the owner of your workspace" (exit 1, 3.3 s). `codex exec --help` states `-` reads the prompt from stdin, and the adapter already passes `-` (`codex_completion.py:334`). The adapter surfaced the CLI's event message in the error, as intended. |
| **agy** | **Not verified.** Authentication succeeded but the account is not eligible ("Eligibility check failed"), so no turn ran. `--help` documents `-p` as an alias for `--print` and says prompts are "read only from -p/--print, -i/--prompt-interactive, or stdin"; whether `-p -` reads stdin is unconfirmed. |
| **cursor** | Not probed. |

Re-run codex, agy and cursor on accounts without these blockers before relying on them. The probe script is a small adapter driver; keep a copy under `tests/` or `scripts/` if it is re-used (it was not added to the repo).

## 4. Work items

### Step 0 — Close the verification gaps
- [ ] `agy`: confirm `-p -` reads stdin (short prompt, ~500 KB prompt, empty stdin must not send a literal `-`). If it does not, use `--input-format stream-json` (see the agy plan, §6.3).
- [ ] codex, cursor: run the 100 KB and ~500 KB probes; record first-output latency and total time.
- [ ] For each backend record the observed model context window and any CLI-side limit below 128k. If a CLI caps below 128k, advertise the lower `context_length` for that backend rather than failing mid-turn.

### Step 1 — `agy` prompt delivery
Implement the agy plan: `-p -`, `stdin=prompt`, delete `PROMPT_LIMIT_BYTES`, the `.cmd`-shim `ValueError` handler and the `command_line_too_long` check.

### Step 2 — Shared no-output watchdog in `proc.py`
`_stream_run` already tracks `last_output_at` from the pump byte counters.
- [x] Add optional `first_output_timeout_s` and `idle_timeout_s` to `run_process`. On expiry, kill the process tree and raise a distinct timeout error (HTTP 504, `code: timeout`) whose message states which limit fired, the silent seconds and the prompt size.
- [x] First-output limit scales with prompt size: `base + per_100kb * (bytes / 100 KB)`. The idle limit is separate and defaults to 300 s for claude and agy only, so it is *longer* than the 120 s first-output base. This is deliberate: every CLI prints startup output within seconds, so the first-output limit only catches hangs before startup, and model prefill and thinking silence is bounded by the idle limit (see Implementation notes).
- [x] Per-agent overrides in `agents.yaml`; defaults chosen from the latencies recorded in Step 0 (claude: 20.9 s for ~570 KB), with generous headroom for thinking models. (Step 0 is incomplete for codex/agy/cursor; defaults are provisional.)
- [x] Keep the existing "cli silent" log lines; they are the data source for tuning.
- [x] Do not retry after an idle kill by default (a turn may not be safe to repeat).

### Step 3 — Give every backend a heartbeat
Without events, silence says nothing. Make non-streaming runs observable.
- [x] `agy`: use stream-json internally even for non-streaming requests so the watcher sees events; remove the `if live:` gate on the idle check, or apply the shared watchdog from Step 2.
- [x] claude (`stream-json`), codex (`--json`): check whether the current non-streaming paths already emit events; if not, move them to the event-producing mode and aggregate.
- [x] Reconcile with the `agy` watcher's existing `IDLE_TIMEOUT_S`/`kill_reason` logic so there is one idle mechanism, not two.

### Step 4 — Acceptance tests
- [ ] Synthetic ~128k-token transcript through each backend (fake CLI in unit tests; real CLI in an opt-in qualification run documented in `docs/gateway-qualification.md`). Done manually for claude only (see §3); codex, agy and cursor still open, and the probe is not yet in the repo or the qualification doc.
- [ ] Multi-turn tool-call round-trip with a growing transcript, confirming latency per turn.
- [x] Real-subprocess test: a script that reads all of stdin (~500 KB) before replying, to exercise pipe-buffer behaviour.
- [x] Watchdog tests: no output → first-output kill; output then silence → idle kill; slow but steady output → not killed; limits scale with prompt size.
- [x] Cancel during stdin feed and during silence kills the child tree.

### Implementation notes (Steps 2-4)
- **Watchdog.** `run_process(..., first_output_timeout_s=, idle_timeout_s=)` samples the stdout+stderr byte counters in the existing poll loop. On expiry it logs `cli stalled limit=<first-output|idle> ...`, kills the tree on the deadline-kill path and raises `ProcessStalled` (like `OutputLimitExceeded`; the overall deadline still returns `timed_out=True`). Each adapter maps it to `CompletionError(status=504, code="timeout")`: `"<Backend> CLI produced no output for <s>s (<limit> limit); prompt <n> bytes"`. The prompt content never appears.
- **Defaults** (`relay.adapters.completion`): first-output = 120 s + 30 s per 100 KiB of prompt bytes sent, for every backend; idle = 300 s (agy's former `IDLE_TIMEOUT_S`) for claude and agy only. Idle is therefore *longer* than the first-output base by design (it carries prefill and thinking silence; see the next bullet). codex and cursor have **no idle default** (`silence_limits(..., idle_default=None)`): their heartbeat is coarse (codex `exec --json` emits an event per finished item; non-streaming cursor emits whole messages), so one long reasoning item could be silent past 300 s while healthy. For them the request deadline governs unless the agent sets `idle_timeout_s`. Both limits apply only when they would expire before the remaining request deadline; otherwise the deadline governs alone. No retry: the runtime repairs only `CompletionInvalid`, never a 504.
- **The first-output limit does not cover model prefill.** Every CLI prints startup output (an init/thread event, a banner or a log line) within seconds, and stderr counts as liveness, so the first-output limit is met by CLI startup long before the model produces anything. Prefill and thinking silence is bounded only by the idle limit (claude, agy: 300 s) or, for codex and cursor by default, by the request deadline alone.
- **Stall decision races a clean exit.** If the child writes its final output and exits within the poll in which a limit expires, `run_process` drops the `ProcessStalled` and returns the normal result, provided the leader had already exited on its own before the kill and bytes arrived after the stall decision.
- **Classified outcomes survive a stall.** On `ProcessStalled`, codex and cursor first check their tool-denied marker (`backend_tool_activity` / `backend_tool_use`), as agy does. agy also records a failed result event and, if the CLI then hangs, classifies it through `parse_completion` (context length, rate limit, ...) instead of the generic 504.
- **Overrides.** Optional `first_output_timeout_s` (replaces the 120 s base; size scaling is still added) and `idle_timeout_s` on each agent in `agents.yaml`. Both must be positive and finite, and booleans are rejected. `builtin_agents.yaml` has no comments to extend, so it is unchanged.
- **Heartbeat per backend** (all four now run an event-stream mode for every request):
  - claude: event stream. Every request, including non-streaming and `output_schema` ones, now runs `--output-format stream-json --verbose --include-partial-messages` (previously `--output-format json` printed only at the end). The final `result` event is the object json mode printed, so `parse_completion` is unchanged. Non-streaming content is captured, not forwarded.
  - agy: event stream. Every request now runs `--output-format stream-json`; previously only streaming and tool turns did. The result event is parsed by the existing `agy_envelope`, and agy's own idle check was removed, so the shared watchdog is the single idle mechanism. The `kill_reason` values are now cancelled/result/malformed/tool. A watchdog kill after a result event still returns the result.
  - codex: event stream, unchanged. `exec --json` emits JSONL `thread.started`/`turn.started`/`item.*`/`turn.completed` events while the turn runs, but only per finished item, so no idle limit by default.
  - cursor: event stream, unchanged. Every request already used `--output-format stream-json`. Non-streaming runs omit `--stream-partial-output`, so events come at message granularity (coarser heartbeat); no idle limit by default.
- **Verified against the real claude CLI:** the stream-json path for non-streaming text and `output_schema` turns, at ~75k and ~400k-800k tokens (§3). This closes the earlier "`--json-schema` with stream-json is only exercised by fake CLIs" risk for claude.
- **Not done:** the same real-CLI check for codex, agy and cursor, and the multi-turn latency item (Step 0).
- **Known tuning data:** with claude in event mode the first event arrives within about a second, so the first-output limit mostly measures CLI startup and prefill/thinking silence falls under the 300 s idle limit. The partial-message stream produces more captured bytes than json mode, so very long outputs count against the 8 MB `max_output_bytes` sooner.

## 5. Deferred
Transcript compaction or session reuse to avoid re-sending context each turn changes request semantics. Plan it separately once Step 0 shows the real per-turn cost. Probing child CPU/network activity to tell "thinking" from "hung" is cross-platform and fragile; not planned.

## 6. Risks
- **Killing healthy slow turns.** Mitigate with size-scaled limits, generous defaults and per-agent overrides; tune from logged `since_output_s` data.
- **A CLI-side cap below 128k.** Surface through advertised `context_length` and `context_length_exceeded`, not a late hang.
- **Unknown hang cause.** Not yet established whether hangs are model prefill, a stuck tool call or a CLI waiting on input. Step 2's error message and logs are meant to separate these.
- **Spend and eligibility blockers** on the test accounts delay Step 0 for codex and agy.
