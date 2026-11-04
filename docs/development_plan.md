# Development Plan

Open work items for relay, newest first. Each item is a self-contained brief: the problem, why it is still open, the options, and the acceptance criteria.

> [!NOTE]
> Completed work items have been moved to [development_plan_archive.md](development_plan_archive.md).

---

## Open Items

### 2. Stream agy tool-bearing turns incrementally; post-fix measurement

**Status:** open — baseline recorded, no post-fix data  
**Area:** `src/relay/adapters/agy_completion.py` (`streaming` at ~650, `_stream_sink` at ~434), `src/relay/adapters/json_stream.py`

#### Problem

The incremental-content fix (archive item 1) is wired into the claude adapter only. agy still sets `streaming = chunk_sink is not None and request.output_schema is None` and passes `sink=None` for schema turns, so by code reading agy tool-bearing turns still emit no content until completion. Not yet confirmed by measurement.

#### Baseline (agy, `relay-8765.log`, pre-fix, last written 2026-11-03)

167/167 requests `stream+tools`; backend first token mean 2.8 s; added buffering latency mean 31.9 s, median 26.6 s, p90 49.6 s, max 152.6 s; 777 heartbeats; 111 waiting warnings; client aborts at ~50.3 s.

#### Next steps

1. Restart the gateway on the current build, run real agy traffic, and re-run the same log analysis. Compare against the baseline.
2. If the delay persists, feed `IncrementalJsonContentDecoder` from agy's text events (`step_update.text_delta` / `content_block_delta`) for schema turns. Confirm which event carries the envelope first.

#### Acceptance Criteria

Post-fix added latency is far below the 31.9 s mean and no ~50 s watchdog aborts occur on the same traffic mix.

### 1. Recoverable quarantine instead of a permanent global lockout

**Status:** open — mitigation shipped, root cause open  
**Area:** `src/relay/gateway/runtime.py` (admission and child lifetime)  
**Discovered:** a client aborted a streamed completion (`status=200 bytes=13 complete=False duration_ms=30549.0`); every later request then returned `503 unavailable` until the process was restarted.

#### Problem

Admission is gated by one process-wide boolean, `GatewayRuntime._healthy`. It is set to `False` when a backend child cannot be confirmed dead within `DRAIN_GRACE_S = 15` seconds (`CompletionTerminationError`), and nothing ever sets it back to `True`. The same handler marks the handle `_quarantined`, and `drain()` deliberately skips `_release` for quarantined handles, so the admission permit is never returned either.

Consequences:

- One aborted or wedged request disables the whole gateway (`503 unavailable`) until the process restarts, regardless of which backend caused it.
- `GET /healthz` keeps returning 200, so a supervisor and the operator both see a healthy process.
- The leaked permit means `active` never returns to zero; a fix that only flipped `_healthy` back would still leave capacity permanently reduced.
- Recovery is documented nowhere in code. It is "restart `relay serve`", discovered by the operator.

#### Why it is still open

Fail-closed is deliberate: an unconfirmed child means a second admit could double-spawn the same agent, so refusing all work is the safe direction. Scoping the refusal further needs a decision on what "safe to serve" means per backend plus a way to prove a stranded child is gone. It was deferred as over-design for a single-operator loopback gateway while the immediate operational defect — the silence — was fixed.

#### Shipped Mitigations

- Quarantine logs `completion termination unconfirmed; quarantining gateway backend=<id>` before flipping state, so the lockout is attributable (`runtime.py:312-320`).
- A failed scratch-directory cleanup no longer disables admission; it warns with the residue path and error type and retains the residue (`runtime.py:321-333`).

#### Options

1. **Per-agent quarantine.** Replace the boolean with a set of quarantined backend ids; admission rejects only those ids. Requires a decision on the permit each quarantined agent leaks.
2. **Prove-dead recovery.** A bounded check (no live backend processes for that agent, scratch dir closed) that clears the quarantine entry instead of requiring a restart.
3. **Operator reset.** An authenticated reset endpoint or CLI verb. Simplest, and keeps the fail-closed default for everything else.
4. **Keep permanent fail-closed.** Document the restart requirement in the operator contract and make the log line state it explicitly.

#### Acceptance Criteria

- A single aborted stream no longer makes unrelated agents unusable, or the restart requirement is documented and the log line is actionable without reading the source.
- Quarantine state is observable without a debugger: an entry log line and a way to inspect current state.
- Capacity accounting stays correct: no permit is retained forever, or its retention is intentional, bounded, and reported.
- The existing fail-closed guarantees still hold: unconfirmed termination quarantines capacity, retains the scratch directory, and the next `start()` returns 503 (`tests/test_gateway_runtime.py:182-204`); probe termination quarantines the gateway while `/v1/models` keeps serving (`tests/test_gateway_discovery.py:456-477`).
- New tests cover the chosen recovery path, including the double-spawn risk the fail-closed rule exists to prevent.

#### Files

`src/relay/gateway/runtime.py` (admission, drain, quarantine), `src/relay/gateway/app.py` (error surface and `/healthz`), `src/relay/cli/commands/serve.py` (only if an operator verb is chosen), `tests/test_gateway_runtime.py`, `tests/test_gateway_discovery.py`.
