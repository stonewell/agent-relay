# Development plan

Open work items for relay, newest first. Each item is a self-contained brief: the problem,
why it is still open, the options, and the acceptance criteria.

## Open items

### 1. Tool-bearing completions return no content until the backend turn finishes

**Status:** partly done — plain (no `tools`) turns stream live since `ce1eb0d` (2026-10-31); requests that carry `tools` still buffer until the turn ends
**Area:** `src/relay/gateway/app.py`, `src/relay/gateway/sse.py`, `src/relay/adapters/` (adapter contract)
**Discovered:** every streamed completion of a slow backend dies at ~30.4 s with `status=200 bytes=890 complete=False duration_ms=30431.5` and no content; retried immediately.

#### Problem

Two paths exist today (verified against the code at `531724d`):

- **Plain turns (no `tools`) stream live.** `runtime.py:252` passes a chunk sink to the adapter only when `request.output_schema is None`; the stream loop (`app.py:650-720`) sends the role chunk and content as the backend produces them. Granularity depends on the backend: claude and agy stream token-level (`text_delta`); cursor only with `--stream-partial-output` (`cursor_completion.py:276`); codex emits one chunk per finished item (`exec --json` events), so effectively at message end.
- **Turns with `tools` buffer the whole turn.** `prepare_envelope` (`envelope.py:201`) sets an output schema whenever `request.tools` is non-empty, so these requests take the `schema is not None` branch (`app.py:637-648`): the client receives only empty-delta `heartbeat_event` frames every `KEEPALIVE_INTERVAL = 5.0` seconds (`app.py:54`, `sse.py:50`) until `handle.result` completes, then the whole response. Agentic clients (multi-file editing, exploring, planning) send tools on nearly every turn, so this is the case that matters, including for 128k contexts.

What streaming cannot fix: time to first token is mostly prefill and thinking. With claude, a 400k-800k token prompt with a tiny reply took 13-20 s end to end. Streaming only recovers the generation time between the first content token and the end of the turn.

Consequences (for the tool-bearing path):

- Time-to-first-token becomes time-to-complete-turn. Any client with a first-token deadline fails on every backend slower than that deadline, regardless of backend health.
- The observed abort is exactly that: `bytes=890` is five 178-byte heartbeat frames and no content; 5 s + 5 x 5 s ≈ 30.4 s.
- Because the caller's first-token guard is per-session and latches on first real content, only a session's first slow turn is killed; later turns on that session complete normally (observed `duration_ms=280869.4 complete=True`). This makes the defect intermittent and easy to misread as flakiness.

#### Why it is still open

The tool-bearing response is a `{content, tool_calls}` envelope that must be complete before `validate_envelope` can check it, and `_execute` (`runtime.py:318-359`) retries once with a repair prompt when it is invalid. Streaming any of it live needs the envelope's `content` string decoded incrementally from partial JSON, and a rule for what happens to streamed text when a repair retry follows.

Hazard in the existing code if streaming were simply enabled for schema requests: `on_chunk` (`runtime.py:238-249`) streams in both attempts, but `streamed` and `handle.streamed_content` are per attempt. A client could see attempt 1's text followed by attempt 2's. Any fix must stream attempt 1 only and keep repair retries buffered; the remainder logic at `app.py:698-705` already drops text that diverges from the final content, but it was written for single-attempt turns.

#### Shipped mitigations

- Live first-content streaming for plain turns (`ce1eb0d`): role chunk and content go out as soon as the backend emits them; the log records `first_content_ms` and `buffered` on the response line (`app.py:108`, `app.py:597-614`).
- Protocol-visible empty-delta heartbeats every 5 s (`sse.py:50`), interval lowered 15.0 → 5.0 (`app.py:54`). Resets idle timers; does not satisfy content-based deadlines.
- Client aborts are logged with a heartbeat count so this failure is attributable instead of silent.

#### Evidence

Claude with `--json-schema` and `--include-partial-messages` (real CLI, envelope schema, `claude` 2.1.318) streams the structured output as a `StructuredOutput` tool block with about 17 `input_json_delta` events: the `content` string arrived word by word from 2.44 s, `tool_calls` at 2.83-3.06 s, and the final `result` at 3.08 s. So the envelope is streamable from claude, `content` first (key order is not guaranteed by the schema). Not verified: agy (probably, via `text_delta` of the JSON text), cursor, codex (item-level only, so no partial output).

#### Options

1. **Stream the envelope's `content` live for tool-bearing turns; emit `tool_calls` complete at the end.** Needs an incremental JSON-string decoder (escapes, `\uXXXX` surrogate pairs, key order), `runtime.py:252` to pass the sink for schema requests, the `schema is not None` branch to use the live loop and then `response_events` for the validated `tool_calls`, and attempt-1-only streaming. Moderate gain (narration before a tool call arrives early); codex gets nothing. Content already sent cannot be retracted if the JSON later breaks.
2. **Emit the role chunk at stream open.** Cheap, but content-based first-token watchdogs do not count it, and it shrinks the 5 s window in which failures can still be plain HTTP errors. Marginal.
3. **Document the constraint.** State that tool-bearing turns are buffered and that clients must not impose a first-token deadline shorter than the slowest backend turn.

Rejected: streaming tool-call deltas early (a client could act on arguments that fail validation later); streaming reasoning deltas (conflicts with `show_raw_agent_reasoning=false` in the codex adapter; defer); sending a dummy non-empty content delta to satisfy watchdogs (pollutes the transcript).

#### Next steps (for later)

1. **Measure before building.** The response log line carries `kind` (for example `stream+tools`), `buffered`, `first_content_ms`, `heartbeats` and `duration_ms`. From real gateway logs compute (a) the share of `stream+tools` requests, (b) how often a tool-bearing turn has narration before the tool call, (c) the gap between the turn end and when a first token could have arrived. If narration before a tool call is rare or the gap is small, option 1 is a poor use of effort; fall back to option 3.
2. **If the numbers justify it, implement option 1 for claude only,** since it is the only backend with verified partial structured output. Probe agy and cursor first, then extend. Keep repair retries buffered.

#### Acceptance criteria

- (Plain turns: met by `ce1eb0d`.) A streamed completion from a backend that emits its first token early delivers that content to the client well before the turn completes, with no duplicate role chunk.
- If option 1 is implemented: a tool-bearing turn from claude delivers `content` deltas before the turn completes, the validated `tool_calls` and `finish_reason: tool_calls` follow, and a repair retry never produces concatenated or duplicated text.
- Client disconnect mid-stream still drains the backend and quarantines on unconfirmed termination, exactly as today.
- Tests cover content arriving before completion, the ordering of role/content/finish chunks, and an abort after content has started.
- The failure in the Problem section is reproducible from the log line alone, and the log line states which cause applied.

#### Files

`src/relay/gateway/app.py` (stream loop, chunk ordering), `src/relay/gateway/sse.py` (frame helpers), `src/relay/gateway/runtime.py` (adapter invocation), `src/relay/adapters/*` (delta contract), `tests/test_gateway_app.py`, `tests/test_gateway_protocol.py`.

### 2. Recoverable quarantine instead of a permanent global lockout

**Status:** open — mitigation shipped, root cause open
**Area:** `src/relay/gateway/runtime.py` (admission and child lifetime)
**Discovered:** a client aborted a streamed completion (`status=200 bytes=13 complete=False
duration_ms=30549.0`); every later request then returned `503 unavailable` until the process was
restarted.

#### Problem

Admission is gated by one process-wide boolean, `GatewayRuntime._healthy`. It is set to `False`
when a backend child cannot be confirmed dead within `DRAIN_GRACE_S = 15` seconds
(`CompletionTerminationError`), and nothing ever sets it back to `True`. The same handler marks the
handle `_quarantined`, and `drain()` deliberately skips `_release` for quarantined handles, so the
admission permit is never returned either.

Consequences:

- One aborted or wedged request disables the whole gateway (`503 unavailable`) until the process
  restarts, regardless of which backend caused it.
- `GET /healthz` keeps returning 200, so a supervisor and the operator both see a healthy process.
- The leaked permit means `active` never returns to zero; a fix that only flipped `_healthy` back
  would still leave capacity permanently reduced.
- Recovery is documented nowhere in code. It is "restart `relay serve`", discovered by the operator.

#### Why it is still open

Fail-closed is deliberate: an unconfirmed child means a second admit could double-spawn the same
agent, so refusing all work is the safe direction. Scoping the refusal further needs a decision on
what "safe to serve" means per backend plus a way to prove a stranded child is gone. It was
deferred as over-design for a single-operator loopback gateway while the immediate operational
defect — the silence — was fixed.

#### Shipped mitigations

- Quarantine logs `completion termination unconfirmed; quarantining gateway backend=<id>` before
  flipping state, so the lockout is attributable (`runtime.py:312-320`).
- A failed scratch-directory cleanup no longer disables admission; it warns with the residue path
  and error type and retains the residue (`runtime.py:321-333`).

#### Options

1. **Per-agent quarantine.** Replace the boolean with a set of quarantined backend ids; admission
   rejects only those ids. Requires a decision on the permit each quarantined agent leaks.
2. **Prove-dead recovery.** A bounded check (no live backend processes for that agent, scratch dir
   closed) that clears the quarantine entry instead of requiring a restart.
3. **Operator reset.** An authenticated reset endpoint or CLI verb. Simplest, and keeps the
   fail-closed default for everything else.
4. **Keep permanent fail-closed.** Document the restart requirement in the operator contract and
   make the log line state it explicitly.

#### Acceptance criteria

- A single aborted stream no longer makes unrelated agents unusable, or the restart requirement is
  documented and the log line is actionable without reading the source.
- Quarantine state is observable without a debugger: an entry log line and a way to inspect current
  state.
- Capacity accounting stays correct: no permit is retained forever, or its retention is intentional,
  bounded, and reported.
- The existing fail-closed guarantees still hold: unconfirmed termination quarantines capacity,
  retains the scratch directory, and the next `start()` returns 503
  (`tests/test_gateway_runtime.py:182-204`); probe termination quarantines the gateway while
  `/v1/models` keeps serving (`tests/test_gateway_discovery.py:456-477`).
- New tests cover the chosen recovery path, including the double-spawn risk the fail-closed rule
  exists to prevent.

#### Files

`src/relay/gateway/runtime.py` (admission, drain, quarantine), `src/relay/gateway/app.py` (error
surface and `/healthz`), `src/relay/cli/commands/serve.py` (only if an operator verb is chosen),
`tests/test_gateway_runtime.py`, `tests/test_gateway_discovery.py`.
