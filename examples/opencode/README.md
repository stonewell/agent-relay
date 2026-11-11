# opencode + Agent Relay

## Install opencode

Download the opencode Windows binary from
<https://github.com/anomalyco/opencode/releases/>.

## Run

1. Start the gateway from the repository root: `uv run relay serve --key 12345678`.
2. From this directory, run `opencode`.

`opencode.json` defines the `relay` provider and the default model and agent.
`.opencode/agents/` defines the `router`, `pro`, and `flash` agents.
