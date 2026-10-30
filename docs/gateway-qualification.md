# Gateway completion qualification

Agent-CLI verification has been removed: relay no longer qualifies, probes or fingerprints an
installed CLI before using it. An agent is usable exactly when it is `enabled` in configuration and
has a gateway adapter, so availability is configuration-driven. `GET /v1/models` lists named
configured model aliases with `null` limits until they are observed. `relay/auto` remains available
for internal routing but is not included in the list. `agy` models are enumerated from `agy models`
(TSV `<slug>\\t<name>`, cached per process, falling back to `agents.agy.models` and then
`("default",)`).

The requirement was that relay run whatever CLI the operator registers and let the operator resolve
CLI-level problems. Pre-flight verification contradicted that: it added a gate that made correctly
configured agents appear to have no models, and every gate had to be re-reviewed whenever the CLI
changed.

## Historical note

Review-based qualification and canary runs previously gated gateway backends: a reviewed profile
record, configuration digest, pinned CLI version/platform/auth mode and opt-in live evidence had to
match before a backend could serve. They are recorded here for context only; the gate was dropped
for the reasons above, none of that mechanism remains, and no procedure in this document should be
followed.

## Operator contract

1. Register the agent in `~/.config/relay/agents.yaml` and set `enabled: true`.
2. Confirm the CLI itself works (installed, authenticated, able to answer); relay does not check.
3. Start `relay serve` and inspect `GET /v1/models`. Limits stay `null` until observed.
4. Send a request. If the CLI is missing, broken or misconfigured, relay reports a detailed error at
   invocation — command, exit code and a redacted stderr tail — on stderr and in the OpenAI-shaped
   HTTP error body, leaving the CLI-level diagnosis to the operator.

## Remaining guardrails

The `--key` bearer check on `/v1/*`, Host and Origin checks, process launching and environment
allowlists remain enforced. These are HTTP-layer and launch controls, not CLI verification.
