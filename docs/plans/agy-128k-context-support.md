# Development Plan: 128k Context Support for `agy` CLI in `relay serve`

**Status:** Proposed / Ready for Implementation  
**Area:** `src/relay/adapters/agy_completion.py`, `tests/test_agy_completion.py`, `src/relay/gateway/`  
**Target:** Enable `relay serve` to reliably process long-context completions (up to 128k tokens, ~300 KB – 500+ KB payloads) with the Antigravity (`agy`) backend CLI across Linux and Windows platforms.

---

## 1. Executive Summary

`agent-relay` provides an OpenAI-compatible gateway (`relay serve`) backed by CLI agents including Antigravity (`agy`), Claude Code (`claude`), OpenAI Codex (`codex`), and Cursor (`cursor`).

Currently, the `agy` adapter (`AgyCompletionAdapter`) passes the user prompt directly as a command-line argument:
```bash
agy -p "<prompt>" --output-format json ...
```

Because operating systems impose strict limits on command-line argument lengths (Linux `MAX_ARG_STRLEN` of 128 KB per argument; Windows `CreateProcess` limit of 32,767 characters), `agy_completion.py` enforces a hardcoded safeguard:
```python
PROMPT_LIMIT_BYTES = 30_000 if sys.platform == "win32" else 120_000
```

Any completion request approaching or exceeding 30 KB (Windows) or 120 KB (Linux) is immediately rejected with HTTP 400 (`code: context_length_exceeded`). Consequently, full 128k token context windows (which produce payloads between 300 KB and 600 KB UTF-8 text) cannot be served by `agy` through Relay.

This plan details the architectural migration from command-line argument passing to **stdin streaming via `-p -`**, aligning `agy` with the architecture already used by `claude`, `codex`, and `cursor` adapters.

---

## 2. Problem Statement & Root Cause Analysis

### 2.1 OS-Level Process Execution Limits

1. **Linux `MAX_ARG_STRLEN` & `ARG_MAX`**:
   - In Linux kernels, `MAX_ARG_STRLEN` limits any single command-line argument (`argv[i]`) passed to `execve(2)` to 32 pages (128 KB = 131,072 bytes).
   - In addition, total `ARG_MAX` bounds the aggregate memory for all arguments and environment variables (typically 2 MB, but scaled dynamically).
   - Passing a prompt larger than 128 KB as a command-line argument triggers kernel error `E2BIG` (Argument list too long), crashing process invocation.

2. **Windows `CreateProcess` Limit (32,767 Characters)**:
   - The Windows Win32 `CreateProcess` API accepts a maximum command-line string length of 32,767 characters (`32 KB`).
   - Furthermore, invoking `agy.cmd` through a batch/cmd shim requires escaping quotes and disallows raw newlines or null bytes, limiting practical argument size to < 8 KB and failing on typical multi-line transcripts.

### 2.2 Relay-Side Artificial Guard in `agy_completion.py`

To prevent OS-level crashes, `src/relay/adapters/agy_completion.py` introduced:
```python
PROMPT_LIMIT_BYTES = 30_000 if sys.platform == "win32" else 120_000
```
In `AgyCompletionAdapter.complete()`:
```python
if len(prompt.encode("utf-8")) > PROMPT_LIMIT_BYTES:
    raise CompletionError(
        "Prompt exceeds CLI argument limit",
        status=400,
        code="context_length_exceeded",
        param="messages",
    )
```

And in `AgyCompletionAdapter.arguments()`:
```python
args = ["-p", prompt, "--output-format", output_format, *COMPLETION_FLAGS]
```

### 2.3 Why 128k Context Fails

- A 128k token prompt transcript (system prompt + multi-turn history + repository context or tool outputs) averages **300 KB – 550 KB** of UTF-8 JSON text.
- Even moderately large requests (e.g., 30k – 60k tokens, ~100 KB – 200 KB) fail immediately:
  - On Windows: blocked if > 30,000 bytes.
  - On Linux: blocked if > 120,000 bytes.
- This creates an artificial capability bottleneck where underlying models (Gemini 2.5 Flash/Pro, Claude Sonnet 3.5, etc.) support 128k to 1M+ tokens, but `relay serve` with `agy` fails on anything beyond ~8k to 30k tokens.

---

## 3. Architectural Solution

### 3.1 Stdin Prompt Piping via `-p -`

The Antigravity CLI (`agy`) natively supports reading the prompt from standard input when passed `-p -`:
```bash
agy -p - --output-format json --agent relay-completion --sandbox ...
```

By piping the prompt through standard input:
1. `argv` contains only static flags and `"-"`. Total command-line length is invariant (~150 bytes), entirely immune to `MAX_ARG_STRLEN` and Windows `CreateProcess` limits.
2. Windows `.cmd` shim newline and quoting restrictions are completely bypassed because stdin streams raw bytes without shell escaping.
3. System throughput and memory efficiency are improved since the OS does not copy large argument strings into kernel execution structures.

### 3.2 Supervision Integration in `proc.py`

`relay.core.platform.proc.run_process` already supports non-blocking standard input streaming via dedicated worker threads:
```python
def feed() -> None:
    with contextlib.suppress(OSError, ValueError):
        if stdin is not None:
            stdin_pipe.write(stdin)
    with contextlib.suppress(OSError, ValueError):
        stdin_pipe.close()
```
The child process's `stdout` and `stderr` are simultaneously drained on concurrent reader threads (`_stream_pump`). This eliminates deadlock risks where a child process produces initial diagnostic output while the parent is writing large chunks to stdin.

### 3.3 Preserving Schema Instructions & Envelope Contract

For structured output turns (`request.output_schema is not None`), `agy` requires appending explicit JSON schema instructions to the prompt (`_agy_schema_instructions(request.output_schema)`).
- With stdin streaming, these schema instructions continue to be appended directly to the `prompt` string piped to `stdin`.
- Because the schema instructions are in stdin rather than in CLI argument flags (unlike Claude's `--json-schema` argument), even massive JSON schemas (e.g., with hundreds of property definitions) will not blow up command-line arguments.

---

## 4. Exact File Modifications

### 4.1 `src/relay/adapters/agy_completion.py`

1. **Remove `PROMPT_LIMIT_BYTES`**:
   - Delete line 72:
     ```python
     - PROMPT_LIMIT_BYTES = 30_000 if sys.platform == "win32" else 120_000
     ```
   - Delete the constant outright (no alias; an unenforced constant is misleading) and remove its import from `tests/test_agy_completion.py` (line 19), which otherwise fails at import.

2. **Update `AgyCompletionAdapter.arguments()`**:
   - Today it merges the schema instructions into the prompt argument (currently `agy_completion.py` lines 623-626). Remove that merge; `arguments()` must not take the prompt at all.
   - Change argument generation to pass `"-"` instead of `prompt`:
     ```python
     @staticmethod
     def arguments(request: CompletionRequest, *, stream: bool = False) -> tuple[str, ...]:
         output_format = "stream-json" if stream else "json"
         args = ["-p", "-", "--output-format", output_format, *COMPLETION_FLAGS]
         if request.model != "default":
             args += ["--model", request.model]
         if request.effort is not None:
             args += ["--effort", request.effort]
         return tuple(args)
     ```
   - Note that schema instructions are no longer merged into an argument; they are handled when preparing `prompt` for stdin.

3. **Update `AgyCompletionAdapter.complete()`**:
   - Assemble `prompt` with schema instructions:
     ```python
     prompt = request.prompt
     if request.output_schema is not None:
         prompt += _agy_schema_instructions(request.output_schema)
     ```
   - Remove the `len(prompt.encode("utf-8")) > PROMPT_LIMIT_BYTES` check.
   - Pass `stdin=prompt` to `run_process()`:
     ```python
     proc = run_process(
         command,
         cwd=cwd,
         env=env,
         stdin=prompt,
         timeout_s=remaining,
     )
     ```
   - The prompt is built once in `complete()` and passed as `stdin=prompt` to the single `run_process(...)` call (currently line 843). `self.arguments(request, stream=live)` (line 823) no longer receives or builds the prompt.
   - Delete the `except ValueError` handler around `resolve_command` (currently lines 827-835). It exists only because a `.cmd` shim cannot forward multi-line prompts in `argv`; with a static `argv` the error cannot occur, and its message would be wrong.
   - Delete the `command_line_too_long(command)` check (currently lines 836-842) and the `command_line_too_long` import (line 24). `argv` is now a fixed ~150 bytes. `claude_completion.py` keeps its own use of the function.
   - Windows: `.cmd` shims forward stdin fine, so no change to `agents.agy.cmd` configuration is required.

### 4.2 `tests/test_agy_completion.py`

1. **Update the existing argument assertions** (find them with `grep -n "arguments(" tests/test_agy_completion.py`; there is no test literally named `test_agy_completion_adapter_arguments`):
   - Assert `args[:4] == ("-p", "-", "--output-format", "json")`.
   - Assert `args_stream[:4] == ("-p", "-", "--output-format", "stream-json")`.
   - Update schema checks to verify that `argv` contains no schema text, and that the schema instructions reach `stdin` instead.

2. **Replace `test_agy_completion_prompt_limit` (line 1127)** — both its oversize and near-limit cases assert the removed 400 — with `test_agy_completion_large_context`:
   - Replace assertions expecting `context_length_exceeded` with assertions verifying that a 128k context prompt (~500 KB) executes successfully:
     ```python
     def test_agy_completion_large_context_piped_via_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
         agent = Agent(cmd="agy")
         adapter = AgyCompletionAdapter(agent)
         large_prompt = "x" * 500_000  # ~500 KB payload
         req = CompletionRequest(large_prompt)
         
         captured_stdin = []
         def fake_run(command, *, stdin=None, **kwargs):
             captured_stdin.append(stdin)
             return agy_response()
             
         monkeypatch.setattr(agy_completion, "run_process", fake_run)
         res = adapter.complete(req, cwd=tmp_path, cancel=threading.Event(), timeout_s=10, max_output_bytes=1024)
         assert res.text == "hello from agy"
         assert captured_stdin[0] == large_prompt
     ```

3. **Delete or rewrite the Windows quoting test** near line 1449 (`'"' * (PROMPT_LIMIT_BYTES - 100)`): it exists to show quoting pushing a prompt past the CreateProcess limit, which no longer applies.

4. **Add one real-subprocess test** (not a `run_process` mock): a small script that reads all of stdin, then replies, fed a ~500 KB prompt. A mock cannot catch the pipe-buffer deadlock described in §6.1. Also add a cancel-during-stdin-feed test using a real process.

---

## 5. Test & Validation Strategy

### 5.1 Unit Testing Matrix

| Test Case | Scope | Expected Behavior |
| :--- | :--- | :--- |
| `test_arguments_has_dash_p_stdin` | `AgyCompletionAdapter.arguments` | `("-p", "-", ...)` produced across all model and stream configurations. |
| `test_schema_instructions_in_stdin` | `AgyCompletionAdapter.complete` | Prompt piped to `stdin` includes `_agy_schema_instructions` when `output_schema` is supplied. |
| `test_500kb_payload_success` | `AgyCompletionAdapter.complete` | A 500 KB prompt completes without raising `CompletionError` or `context_length_exceeded`. |
| `test_cancel_during_stdin_feed` | `AgyCompletionAdapter.complete` | Process cancellation cleanly interrupts stdin thread and kills child process tree. |

### 5.2 Integration & Gateway Qualification

1. **Gateway Transcript Verification**:
   - Start gateway: `relay serve --agent agy`.
   - Send `POST /v1/chat/completions` with 128k tokens of conversation history (generated synthetically with repetitive context or large source file attachments).
   - Verify HTTP 200 response with correct SSE delta streaming.

2. **Piping & Pipe Buffer Integrity**:
   - Verify that when streaming large inputs (500 KB+), the Linux pipe buffer (default 64 KB capacity) does not block or deadlock.
   - Confirm that `feed()` closes `stdin_pipe` cleanly so `agy` detects EOF and initiates model turn.

---

## 6. Risk Analysis & Fallbacks

### 6.1 Process Buffering & Deadlocks
- **Risk**: If the child process emits stdout warnings or starts writing events before consuming all of stdin, a full pipe buffer (64 KB on Linux) could cause `stdin.write` to block while `proc` blocks on stdout write.
- **Mitigation**: `relay.core.platform.proc._stream_run` runs `feed()`, `stdout_pump`, and `stderr_pump` in separate concurrent threads (`threading.Thread(target=feed, daemon=True)`). Both pipes are pumped asynchronously, preventing deadlock.

### 6.2 Turn Execution Timeouts
- **Risk**: 128k token context requests have significantly longer time-to-first-token (TTFT) and processing duration (e.g. 30s to 120s depending on model load).
- **Mitigation**:
  - `IDLE_TIMEOUT_S = 300.0` in `agy_completion.py` allows generous silent periods.
  - When `stream=True`, `agy` emits step and stream events that keep the connection active.
  - Heartbeat events (`KEEPALIVE_INTERVAL = 5.0`) in `relay/gateway/app.py` prevent client transport disconnection.

### 6.3 Backwards Compatibility with Installed CLI
- **Risk**: The installed `agy` CLI might not read the prompt from stdin for `-p -`. If it treats `-` as literal prompt text, requests would silently send a one-character prompt.
- **Status: UNVERIFIED.** This plan depends on it and it has not been confirmed end to end.
  - `agy` 1.5.0 `--help` documents `-p` as an alias for `--print`, and its error text says prompts are "read only from -p/--print, -i/--prompt-interactive, or stdin". That suggests stdin is supported, but does not prove `-p -` reads it.
  - A probe (`"reply with exactly: pong" | agy -p - --output-format json`) failed before any model turn with `status: ERROR` ("Eligibility check failed: account is not eligible for Antigravity"), so stdin handling was not exercised.
- **Required before implementation (Step 0)**: on a machine with an eligible account, confirm all three:
  1. A short prompt on stdin returns the expected reply.
  2. A ~500 KB prompt on stdin completes.
  3. `-p -` with empty stdin fails (rather than treating `-` as the prompt).
  Record the `agy --version` and outputs here.
- **Fallback if `-p -` does not read stdin**: `agy` also supports `--input-format stream-json` (one NDJSON message per line on stdin, requires `--output-format stream-json`). Its message shape would need to be confirmed and the adapter's non-streaming path would have to parse stream output.

---

## 7. Implementation Roadmap & Checklist

- [ ] **Step 0**: Verify `agy -p -` reads stdin (see §6.3); record version and outputs. Do not start Step 1 until this passes, or the stream-json fallback is chosen.
- [ ] **Step 1**: Edit `src/relay/adapters/agy_completion.py`:
  - [ ] Switch `arguments()` to `["-p", "-", ...]` and drop the schema merge from it
  - [ ] Delete `PROMPT_LIMIT_BYTES` and its check in `complete()`
  - [ ] Delete the `.cmd`-shim `ValueError` handler and the `command_line_too_long` check/import
  - [ ] Pass `stdin=prompt` in `run_process(..., stdin=prompt)`
- [ ] **Step 2**: Edit `tests/test_agy_completion.py`:
  - [ ] Remove the `PROMPT_LIMIT_BYTES` import; replace `test_agy_completion_prompt_limit`; delete/rewrite the Windows quoting test
  - [ ] Update CLI argument assertions to check `"-p", "-"`
  - [ ] Update mock runs to verify `stdin` piping
  - [ ] Add 128k payload test case and a real-subprocess 500 KB stdin test
  - [ ] Check that `docs/gateway-qualification.md` does not describe the old limit
- [ ] **Step 3**: Run pytest suite:
  - [ ] `pytest tests/test_agy_completion.py`
  - [ ] `pytest tests/test_gateway_app.py`
- [ ] **Step 4**: Verify against running `relay serve` with 128k token synthetic payload.
