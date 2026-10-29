# Gateway completion qualification

Gateway v1 is implemented as a restricted, buffered Chat Completions service. Its production
completion qualification list contains one reviewed **plain-text-only** record: Claude Code
2.1.312 on Windows with subscription authentication and the exact revision-2 configuration
digest recorded below. Matching profiles can serve text completions; every other profile stays
unavailable. An installed Claude CLI alone does not create an available model. Codex remains excluded: the
recorded read-only profile read outside its working directory (spike 2.31).

The implementation and offline tests do **not** establish M7 release qualification. The operator
authorized gateway canaries: the initial two calls and the two final-revision checks passed as recorded below. No growing-history cache
experiment or pinned OpenCode edit-and-test session was run. Existing deferred MVP live/remote
validation remains deferred; the gateway authorization did not resume those separate checks.

## Task 5.0: repository evidence and decisions

- `ClaudeAdapter.coordinate` supplies the candidate restriction flags: tools disabled, empty
  strict MCP configuration, slash commands/settings sources disabled, no session persistence.
  Existing coordinator canaries support investigating that candidate; their qualifications
  are not copied to completion records because the prompt/schema invocation differs.
- `tests/fixtures/recorded/claude_plain.json` demonstrates the final JSON result and separate
  uncached input, cache-read, cache-write and output usage buckets. The completion parser
  preserves them and leaves usage unavailable when required counters are absent.
- The existing display progress channel is lossy and sanitized. v1 therefore buffers final
  output and emits SSE after validation, with keepalives while awaiting it. No native token
  streaming, native output-token limit or cache-hit improvement is asserted.
- Ordinary task routing has permission eligibility rules that are not completion
  qualification. Gateway routing filters completion candidates separately; all aliases share
  the same agent capacity. No worktree/run/task state is created.
- Shared process capture now bounds unterminated lines and fails if pipe drainage cannot be
  confirmed. Runtime admission remains held through HTTP delivery, and uncertain termination
  quarantines capacity and stops admission. These controls are tested with local subprocesses.

Unknowns requiring later live evidence: broader restriction reliability beyond the observed
profile; provider/managed-policy customization; nonempty tool envelope reliability on the
pinned client schemas; native token controls; auth mode differences; cache counters/latency
across growing transcripts. No native token guarantee is claimed. Model context/output limits
are not part of qualification: they are observed from the CLI at runtime (see
[Model discovery](#model-discovery)).

## Observed live evidence (2026-10-29)

**Initial revision-1 run:** `RELAY_LIVE_GATEWAY=1 uv run pytest -q -s tests/test_gateway_live.py`: **2 passed in 12.77 s**,
Claude Code 2.1.312, Windows, subscription authentication. Plain and tool-envelope calls returned
valid responses; neither exposed the random inside/outside secrets nor triggered the write,
shell, loopback-network, hook, MCP or project-instruction traps. One observation per mode is
not a general confinement proof and does not qualify nonempty client-tool envelopes.

| Mode | Prompt tokens | Completion tokens | Cached reads | Cache writes |
|---|---:|---:|---:|---:|
| Plain | 2784 | 737 | 649 | 2133 |
| Tool envelope | 3350 | 371 | 0 | 3348 |

Prompt totals include uncached input plus the separate read/write buckets; cache writes are
not counted as cached reads. These different prompts do not establish a cache hit rate for
resending conversation history.

The original whole-file fingerprint changed between modes: plain
`50ef172bbcdf9470526b82f28594676a81b1c66032a2fffd29e24de4fc2194d3`, envelope
`edaa846bbce34a43b7ac7b634a51050df7aa4398b17362f565e76eb56a62309b`. Non-billable inspection
found ordinary `.claude.json` bookkeeping changes; a recent CLI backup differed only in
plugin-usage counters. Earlier backups also changed project timing/token metrics, tips and
feature caches. Claude documents this file as self-written account/MCP/project/global state,
separate from its user settings file ([official settings documentation](https://code.claude.com/docs/en/settings)).

Revision 2 excludes the narrowly evidenced observational fields and retains unknown and
security-relevant state. Offline regressions verify both sides of that boundary. Three repeated
non-billable revision-2 probes first returned the same fingerprint. Feature-cache changes,
new project identities and managed-policy changes can still invalidate qualification deliberately.

**Final revision-2 run:** the same authorized canary command passed **2 tests in 25.22 s**.
Both modes matched their pre-call and post-call profiles, and matched each other:

- Claude Code `2.1.312`, platform `win32`, authentication `subscription`.
- Configuration digest `49d8e0ee09a8f4f1318b17eedebc6c8d2c85885f082989334416aa3d393ab2d9`.
- Both modes again returned valid responses without triggering the restriction traps.

| Final mode | Prompt tokens | Completion tokens | Cached reads | Cache writes |
|---|---:|---:|---:|---:|
| Plain | 2784 | 543 | 649 | 2133 |
| Tool envelope | 3350 | 436 | 1121 | 2227 |

These observations establish tested behavior for this candidate invocation/profile, not general
filesystem confinement or nonempty client tool calls. **Independent review approved only plain-text promotion** for the exact final profile. The
record has `supports_tools=False`. Nonempty client-tool envelopes remain unqualified; tool
requests fail closed and `opencode-config` publishes these models with `tool_call: false`.
The record assumes the reviewed
managed-policy and keychain/backend state remains unchanged; those unobservable inputs require
operator requalification if changed, and the record makes no general sandbox claim.

Final offline evidence: the full Windows snapshot passed **1,695 tests, 16 skipped** in
1,257.71 s. The subsequent affected-file checks passed **108 tests, 2 skipped** in 9.80 s.
After exact plain-text promotion, 76 affected gateway tests passed, including text admission,
tool refusal and profile-mismatch regressions. Ruff lint, full Pyright, formatting of the
changed Python files and package build passed again.
A repository-wide format check still reports the existing `tests/test_tui.py` formatting
issue; that unrelated file was not changed by gateway work. Independent review addressed
Unicode handling, bounded schema validation and profile stability before the final canaries.

## Model discovery

Qualification records gate only the profile and tool support; they contain no model ids or
limits. The Claude CLI has no model-listing command, so the gateway asks the CLI itself. Each
configured alias (`relay serve --model ALIAS`, repeatable; default `default`, `sonnet`, `opus`,
`fable`) is probed once with a minimal prompt through the same qualified `complete()` path as
a request: profile recheck before launch, fresh scratch directory, isolated environment,
restriction flags, cancellation and a 120 s deadline. The JSON result's `modelUsage` entry
(see `tests/fixtures/recorded/claude_plain.json`) names the model that ran and reports its
`contextWindow` and `maxOutputTokens`. Only a single entry with positive integer limits counts.

- **Cost.** Every probe is a short billed model call on the configured account.
- **When.** The gateway probes lazily on the first `/v1/models` or `/v1/models/{id}` request,
  never at startup or for chat requests. Concurrent requests share one discovery run, and the
  gateway does not re-probe an alias it already tried. `relay opencode-config --refresh`
  probes explicitly; without `--refresh` it reads only the cache and names undiscovered models
  on stderr.
- **Cache.** Observations are written atomically to `~/.config/relay/gateway-models.json`,
  keyed by the full completion profile (agent, CLI version, platform, auth mode, configuration
  digest). A CLI upgrade or configuration change is a cache miss; a missing or corrupt file is
  ignored and rewritten. Deleting the file is always safe.
- **Fail-soft.** An alias whose probe fails (unknown alias, auth, timeout, ambiguous or missing
  `modelUsage`) is omitted from the catalog and from generated client configuration; it is
  never listed with null or invented limits and is not cached, so a later run retries it.
  Unconfirmed probe termination quarantines the gateway as a request would.
- **Meaning.** The limits are the CLI's reported model limits. Gateway transcript wrapping, the
  CLI's own prompt and schema overhead are not subtracted, and native `max_tokens` remains
  unsupported.

## Inspect a candidate without a model call

After installing `uv sync --extra serve`, run from the repository:

```powershell
uv run python -c "from relay.core.registry import load_config; from relay.adapters.claude_completion import ClaudeCompletionAdapter, profile_json; print(profile_json(ClaudeCompletionAdapter(load_config(project_root=None).config.agents['claude'])))"
```

This runs a bounded `--version` probe and fingerprints the candidate; it does not call a model,
print credentials or install a qualification. The fingerprint includes the resolved command,
restriction/structured-output template, complete forwarded environment values and known
user/config-home settings-file content hashes. Profile revision 2 canonicalizes `.claude.json`
and excludes only the explicitly listed observational startup/tip/plugin-usage counters and
per-project timing/token/session metrics in `claude_completion.py`. It preserves project
identities, trust/permission/MCP/auth values, feature-cache contents and unknown fields. Only
the aggregate digest is printed. Other changes invalidate the record. Credentials changing
can also require requalification.
The digest cannot detect every managed policy or keychain/backend configuration change;
reviewers must document those assumptions and requalify when they change.

## Opt-in evidence collection

Only after explicitly deciding to run live gateway calls:

```powershell
$env:RELAY_LIVE_GATEWAY = '1'
uv run pytest -q -s tests/test_gateway_live.py
Remove-Item Env:RELAY_LIVE_GATEWAY
```

The two tests use the configured Claude account and make one call per mode. They inject a
test-only candidate record and test reads inside/outside scratch, writes, shell, a loopback
network trap, project hooks, MCP and `CLAUDE.md`. They check profile equality before/after each call and across modes, and print only those profiles/mode/observed usage,
never raw prompts or provider stderr. Nothing modifies production `QUALIFICATIONS`.

Passing means those traps were not observed during those calls, not a proof of all filesystem
or network confinement. Review startup/user/managed customization separately without replacing
the operator's credentials. Verify actual nonempty client-tool envelopes and the full pinned
OpenCode tool/result loop. Record CLI/OS/auth/config identity, invocation modes, observed
limitations and a reproducible evidence reference. Cache experiments
must use growing rendered histories including a tool/result turn and report missing counters
as unknown; they do not authorize session resume.

## Reviewed promotion

Add an explicitly reviewed `CompletionQualification` to
`src/relay/adapters/completion.py:QUALIFICATIONS`, using the exact inspected profile and an
evidence reference. Set `supports_tools=True` only after the envelope mode and client schemas
qualify. Records carry no model limits; those come from discovery for the matching profile.
Native `max_tokens` remains unsupported in this implementation.

Restart the gateway after promotion/config changes. The backend rechecks its profile before
every launch, including repair: configuration files are re-hashed each time, while the
`--version` result is reused for up to 30 s if the environment and the resolved executable
files' size/mtime are unchanged. Unknown versions or configuration changes fail closed; do not
add a wildcard record, copy coordinator evidence as completion evidence, or expose a CLI flag
that disables qualification. Review the optional SDK/client compatibility gates before claiming
M7. Remote serving, session resume, `/v1/responses` and incremental envelopes remain deferred.
