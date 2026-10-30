# Agent Relay

Agent Relay runs a local OpenAI-compatible completion gateway for configured agent CLIs.

## Install and run

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run relay serve --key local-development-token
```

Install and authenticate at least one supported agent CLI first. Claude and Codex are enabled by
default. The server listens on `http://127.0.0.1:8765/v1`, accepts loopback addresses only, and
runs in the foreground. Press Ctrl+C to stop it. Run `uv run relay serve --help` to see all
options.

Requests require the bearer token passed with `--key` or `RELAY_GATEWAY_TOKEN`. If neither is set,
Relay generates a token and prints it once to stderr when the server starts.

In another terminal, list available models:

```sh
curl http://127.0.0.1:8765/v1/models \
  -H 'Authorization: Bearer local-development-token'
```

Send a chat completion request:

```sh
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Authorization: Bearer local-development-token' \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude/sonnet","messages":[{"role":"user","content":"Review this code"}]}'
```

## Configuration and models

Relay merges built-in settings with optional user settings from
`~/.config/relay/agents.yaml`. It does not load project `.agents/agents.yaml` files. Cursor is
disabled by default; enable it in the user config like this:

```yaml
agents:
  cursor:
    enabled: true
```

Configuration can set default concurrency and timeout, and per-agent command, environment,
concurrency, model aliases, and automatic-routing preferences. See
[gateway qualification notes](docs/gateway-qualification.md) for backend behavior and operating
limits.

`GET /v1/models` lists model families as `<agent>/<family>`, such as `claude/sonnet` or
`cursor/gemini-3.8-flash`. Reasoning variants such as `-low` and `-high` share one listed family;
their exact IDs remain usable in completion requests. Models come from built-in defaults or the
configured CLI. You can limit the models offered by repeating `--model`:

```sh
uv run relay serve --model sonnet --model opus
```

`relay/auto` is available for automatic routing but is not listed as a named model. The first
`GET /v1/models` request probes uncached aliases to discover model limits; this may incur model
usage.

The gateway accepts OpenAI-compatible `POST /v1/chat/completions` requests. Streaming and tool
proposals are supported; proposed tools are returned to the client and never executed by Relay.
Reasoning effort depends on the backend.

## Development

```sh
uv run pytest -q
uv run ruff check .
uv run pyright src
uv build
```
