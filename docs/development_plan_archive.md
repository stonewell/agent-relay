# Development Plan Archive

Completed work items for relay. Active and open items are maintained in [development_plan.md](development_plan.md).

---

## Completed Items

### 1. Tool-bearing completions return no content until the backend turn finishes

**Status:** Completed — live streaming of content deltas for tool-bearing turns implemented (Option 1)  
**Area:** `src/relay/gateway/app.py`, `src/relay/gateway/sse.py`, `src/relay/gateway/runtime.py`, `src/relay/adapters/` (claude, agy), `src/relay/adapters/json_stream.py`  
**Discovered:** Every streamed completion of a slow backend dies at ~30.4 s with `status=200 bytes=890 complete=False duration_ms=30431.5` and no content; retried immediately.

#### Problem

Two paths existed prior to the fix:

- **Plain turns (no `tools`) stream live.** `runtime.py:252` passed a chunk sink to the adapter only when `request.output_schema is None`; the stream loop (`app.py:650-720`) sent the role chunk and content as the backend produced them. Granularity depends on the backend: claude and agy stream token-level (`text_delta`); cursor only with `--stream-partial-output` (`cursor_completion.py:276`); codex emits one chunk per finished item (`exec --json` events), so effectively at message end.
- **Turns with `tools` buffered the whole turn.** `prepare_envelope` (`envelope.py:201`) sets an output schema whenever `request.tools` is non-empty, so these requests took the `schema is not None` branch (`app.py:637-648`): the client received only empty-delta `heartbeat_event` frames every `KEEPALIVE_INTERVAL = 5.0` seconds (`app.py:54`, `sse.py:50`) until `handle.result` completed, then the whole response. Agentic clients (multi-file editing, exploring, planning) send tools on nearly every turn, so this is the case that matters, including for 128k contexts.

What streaming cannot fix: time to first token is mostly prefill and thinking. With claude, a 400k-800k token prompt with a tiny reply took 13-20 s end to end. Streaming only recovers the generation time between the first content token and the end of the turn.

Consequences (for the tool-bearing path):

- Time-to-first-token becomes time-to-complete-turn. Any client with a first-token deadline fails on every backend slower than that deadline, regardless of backend health.
- The observed abort is exactly that: `bytes=890` is five 178-byte heartbeat frames and no content; 5 s + 5 x 5 s ≈ 30.4 s.
- Because the caller's first-token guard is per-session and latches on first real content, only a session's first slow turn is killed; later turns on that session complete normally (observed `duration_ms=280869.4 complete=True`). This makes the defect intermittent and easy to misread as flakiness.

#### Key Constraints and Hazards Addressed

The tool-bearing response is a `{content, tool_calls}` envelope that must be complete before `validate_envelope` can check it, and `_execute` (`runtime.py:318-359`) retries once with a repair prompt when it is invalid. Streaming any of it live requires the envelope's `content` string decoded incrementally from partial JSON, and a rule for what happens to streamed text when a repair retry follows.

Hazard addressed: `on_chunk` in earlier code streamed on every attempt, but `streamed` and `handle.streamed_content` were per attempt. To prevent attempt 1 (repair retry) from appending to or corrupting attempt 0 output, the runtime sets `stream_chunks = attempt == 0`. Repair retries are strictly buffered, preserving attempt 0 content and preventing concatenated or duplicated text.

#### Shipped Mitigations

- Live first-content streaming for plain turns (`ce1eb0d`): role chunk and content go out as soon as the backend emits them; the log records `first_content_ms` and `buffered` on the response line (`app.py:108`, `app.py:597-614`).
- Protocol-visible empty-delta heartbeats every 5 s (`sse.py:50`), interval lowered 15.0 → 5.0 (`app.py:54`). Resets idle timers; does not satisfy content-based deadlines.
- Client aborts are logged with a heartbeat count so this failure is attributable instead of silent.

#### Evidence

Claude with `--json-schema` and `--include-partial-messages` (real CLI, envelope schema, `claude` 2.1.318) streams the structured output as a `StructuredOutput` tool block with about 17 `input_json_delta` events: the `content` string arrived word by word from 2.44 s, `tool_calls` at 2.83-3.06 s, and the final `result` at 3.08 s. So the envelope is streamable from claude, `content` first (key order is not guaranteed by the schema).

#### Measurement Findings

Analysis of gateway production logs (`~/.config/relay/logs/relay-8765.log`, **agy backend traffic**) confirmed that tool-bearing turns dominate traffic and impose massive buffering delays:

- **100% of requests are `stream+tools`** (167 / 167 requests).
- **100% of tool-bearing turns have narration / assistant reasoning** (mean 4,124 completion tokens).
- **Backend first-token emitted in mean 2.8 s**, but gateway buffering imposed a **mean 31.9 s latency penalty** (median 26.6 s, p90 49.6 s, max 152.6 s) before clients received any content.
- **777 empty-delta heartbeats** and **111 stream waiting warnings** were logged during buffered waits.
- **Client watchdog aborts observed at ~50.3 s** when buffering exceeded client timeouts.

#### Implemented Solution (Option 1)

> [!NOTE]
> Implemented for the **claude** adapter only. The agy adapter still buffers schema turns (see open item 2 in `development_plan.md`); the baseline above was measured on agy before this fix.

Option 1 was implemented across the adapter and gateway layers to stream `content` deltas live while holding `tool_calls` until turn completion:

- `src/relay/adapters/json_stream.py`: `IncrementalJsonContentDecoder` for live parsing of top-level content from partial JSON chunks, supporting Unicode escapes, surrogate pairs, arbitrary key ordering, nulls, and nested content.
- `src/relay/adapters/claude_completion.py`: Structured streaming hookup via `_stream_sink` delivering partial JSON chunks into the incremental decoder.
- `src/relay/gateway/runtime.py`: `stream_chunks` flag ensuring repair retries (attempt 1) are buffered, preserving attempt 0 content.
- `src/relay/gateway/sse.py`: `format_tool_calls` helper for formatting completed tool-call events at turn end.
- `src/relay/gateway/app.py`: Unified live streaming loop delivering content deltas live, completed tool calls at turn end, and finish reason `tool_calls`.

#### Test Verification and Review Outcomes

- **37 unit and integration tests passing** in `tests/test_incremental_json.py` covering incremental parsing, Unicode escape decoding, surrogate pairs, arbitrary key ordering, nulls, and end-to-end SSE tool-call streaming.
- **157 gateway and completion regression tests passing** across `tests/test_gateway_*.py` and `tests/test_*completion.py`.
- **Independent Pro code review:** Approved / Sound.

---

### 2. AGY CLI 128k Context Support via stdin

**Status:** Completed — stdin prompt delivery implemented  
**Area:** `src/relay/adapters/agy_completion.py`, `tests/test_agy_completion.py`  
**Discovered:** Large agentic prompts (128k context, e.g. 100k-500k bytes) failed with `context_length_exceeded` because the prompt was passed as a command-line argument (`-p "<prompt>"`).

#### Problem

The AGY CLI adapter originally formatted CLI arguments as `"-p", prompt` on the process command line. On Linux and Windows, command line lengths are bounded by operating system argument limits (CreateProcess 32KB on Windows, execve limits on Linux). To prevent crash loops, an artificial gate `PROMPT_LIMIT_BYTES` (30 KB on Windows, 120 KB on Linux) rejected prompts exceeding this limit with `context_length_exceeded` before spawning.

For modern 128k token contexts (hundreds of kilobytes of prompt text, multi-file editing transcripts, and large tool definitions), this limit broke long-running sessions.

#### Implemented Solution

- **`arguments()`**: Changed argument structure from `"-p", prompt` to `"-p", "-"` so the AGY CLI reads prompt text from standard input (stdin).
- **`complete()`**:
  - Removed the `PROMPT_LIMIT_BYTES` limit check so large prompts pass through cleanly.
  - Piped the prompt directly to standard input via `stdin=prompt` in `run_process(...)`.
  - When `request.output_schema is not None`, appended schema instructions to `prompt` before sending via stdin.
  - Updated command-line length diagnostics and docstrings to reflect standard input piping.

#### Test Verification Outcomes

- **80 unit and regression tests passing** in `tests/test_agy_completion.py`.
- Verified that a **500 KB prompt (> 128k tokens)** succeeds without error, is delivered via stdin, and is not embedded in the command-line argument list.
- Verified schema instructions append properly to stdin under structured outputs.
