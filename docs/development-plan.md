# Development plan

Open work items for relay, newest first. Each item is a self-contained brief: the problem,
why it is still open, the options, and the acceptance criteria.

## Open items

### 1. Completions return no content until the backend turn finishes

**Status:** open — diagnosed, no relay-side fix landed
**Area:** `src/relay/gateway/app.py`, `src/relay/gateway/sse.py`, `src/relay/adapters/` (adapter contract)
**Discovered:** every streamed completion of a slow backend dies at ~30.4 s with `status=200 bytes=890 complete=False duration_ms=30431.5` and no content; retried immediately.

#### Problem

The gateway buffers the whole backend turn. Backend adapters execute synchronously to completion inside a worker thread (`runtime.py:219-235`, `asyncio.to_thread(invoke_and_validate)`), and the assistant role chunk plus content/tool-call chunks are emitted only after `handle.result.done()` (`app.py:492-495`). While the turn runs, the client receives nothing but empty-delta `heartbeat_event` frames every `KEEPALIVE_INTERVAL = 5.0` seconds (`app.py:45`, `sse.py:50`).

Consequences:

- Time-to-first-token becomes time-to-complete-turn. Any client with a first-token deadline fails on every backend slower than that deadline, regardless of backend health.
- The observed abort is exactly that: `bytes=890` is five 178-byte heartbeat frames and no content; 5 s + 5 x 5 s ≈ 30.4 s.
- The deadline does not belong to this repo. The caller is the `opencode-runtime-fallback` plugin's first-token watchdog, default `timeout_seconds: 30` (dist `chunk-0wvy6bk9.js:10`), armed only while the session has not yet received content (`v1.js:1057`, `!sessionFirstTokenReceived`). Empty-delta heartbeats never clear it, which is why the keepalive pass cannot help here.
- Because the guard is per-session and latches on first real content, only a session's first slow turn is killed; later turns on that session complete normally (observed `duration_ms=280869.4 complete=True`). This makes the defect intermittent and easy to misread as flakiness.

#### Why it is still open

Fixing it relay-side means adapters must yield deltas as the backend produces them, replacing the single `CompletionResult` return with an incremental stream. That changes the adapter contract for every backend (`agy`, `claude`, `codex`, `cursor`) and how `validate_envelope` and schema repair run, which cannot happen before the envelope exists. The keepalive pass only stopped clients that treat any SSE frame as liveness.

#### Shipped mitigations

- Protocol-visible empty-delta heartbeats every 5 s (`sse.py:50`), interval lowered 15.0 → 5.0 (`app.py:45`). Resets idle timers; does not satisfy content-based deadlines.
- Client aborts are logged with a heartbeat count (`app.py:496-503`) so this failure is attributable instead of silent.

#### Options

1. **Stream the first content delta.** Adapt the adapter contract to yield deltas, emit the role chunk and first content chunk as soon as the backend emits them, and keep buffering only for envelope validation. Correct fix, largest change.
2. **Emit the role chunk at stream open.** Cheap: send `{"role": "assistant"}` before the first heartbeat. Only helps clients that gate on stream initialization rather than on content; does not help a content-based watchdog.
3. **Client-side timeout.** Configure the caller's first-token watchdog (`timeout_seconds: 0` or a large value) or move its consumers off backends slower than the deadline. No relay change; leaves the latent failure for every other client.
4. **Document the constraint.** State that backends are non-streaming and that clients must not impose a first-token deadline shorter than the slowest backend turn.

#### Acceptance criteria

- A streamed completion from a backend that emits its first token early delivers that content to the client well before the turn completes, with no duplicate role chunk.
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
