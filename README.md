# Agent Relay

Agent Relay runs a local OpenAI-compatible completion gateway for configured agent CLIs.

## Install and run

Use Python 3.11 or newer and `uv`. Install the project and start the foreground server:

```sh
uv sync
uv run relay serve
```

The default listener is `http://127.0.0.1:8765/v1`. The server accepts loopback addresses only,
needs no Git repository, and loads built-in and user configuration at startup. It does not load
the current project's `.agents/agents.yaml`. Stop it with Ctrl+C.

Use `--host`, `--port`, `--key`, `--concurrency`, `--timeout`, `--max-request-bytes`,
`--max-output-bytes`, and repeatable `--model` options to configure the listener, token, resource
limits, and exposed aliases. Set `RELAY_GATEWAY_TOKEN` to choose a bearer token, or pass `--key`;
if neither is provided, the server generates a token and prints it once to stderr. Send that token
as an Authorization bearer token with gateway requests. The token is never written to project
files.

## Models and requests

`GET /v1/models` lists named backend-specific aliases as `<agent>/<alias>` for enabled agents with
a completion adapter (`claude`, `agy`, `codex`, and `cursor`). Cursor is disabled by default.
Bare-agent defaults and `relay/auto` remain available for internal routing, but are not published
in the model list. Add an enabled backend to `~/.config/relay/agents.yaml` to expose it. For example:

```yaml
agents:
  cursor:
    enabled: true
    models:
      preferred: gemini-3.8-flash-low
```

Claude aliases include `sonnet`, `opus`, and `fable`. Codex aliases come from visible slugs in
`codex debug models` JSON, and Cursor aliases come from account-listed IDs in `agent models`.
These bounded, read-only inventory queries are cached per process. If a query fails, Relay warns
and falls back to configured model IDs, then `default`; this does not disable the backend. `agy`
aliases come from `agy models` and fall back to configured values, then `default`. An explicit
`--model` selection bypasses automatic inventory.

Aliases initially appear without context or output limits. On the first `/v1/models` request,
each uncached alias is probed once with a minimal prompt; this is a short billed model call. The
CLI-reported model and limits are cached in `~/.config/relay/gateway-models.json`. Probe failures
are non-fatal and leave the alias listed without limits. Reasoning effort and output-token support
depend on the backend. Unsupported effort values return HTTP 400, tool proposals are validated and
returned to the client without execution, and unknown model limits remain unknown.

Send OpenAI-compatible chat completion requests to `POST /v1/chat/completions`. For example:

```json
{"model":"claude/sonnet","messages":[{"role":"user","content":"Review this code"}]}
```

An agent is served when it is enabled in configuration and has a gateway adapter. Cursor uses
fixed CLI-mediated read-only and tool-denial controls with private scratch space and denial hooks;
these controls do not provide operating-system filesystem isolation. Relay does not check CLI
version, platform, or authentication mode before a request. If a CLI fails, Relay reports the
command, exit code, and a redacted stderr tail on stderr and in the HTTP error response. See the
[gateway qualification notes](docs/gateway-qualification.md) for the operator contract.

## Development

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
```
