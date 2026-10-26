# Codex task routing

These instructions configure Codex coordination in this project. Agent definitions
are in `.codex/agents/`; `.codex/config.toml` sets the coordinator and concurrency.

## Delegation

The primary Codex agent must delegate substantive implementation, investigation,
testing, and review work to the appropriate custom subagent below. Keep trivial
questions, classification, integration, and final reporting in the primary agent.
Use parallel subagents when independent work can proceed concurrently. Subagents
must complete their assigned work directly and must not delegate further.

Classify each work item by category and complexity before choosing a role:

| Category / complexity | Role | Model | Reasoning |
| --- | --- | --- | --- |
| Bounded searches, navigation, extraction; low | `scout` | `gpt-6-luna` | high |
| Small exact edits, formatting, clear docs; low | `mechanical` | `gpt-6-luna` | high |
| Features, clear bugs, routine refactors; medium | `implementer` | `gpt-6.1-sol` | medium |
| Architecture, unclear root causes, concurrency, security, migrations; high | `specialist` | `gpt-6-astra` | high |
| Test authoring, reproduction, validation; medium | `tester` | `gpt-6.1-sol` | high |
| Independent correctness/security review; high | `reviewer` | `gpt-6-astra` | high |

Complexity overrides the ordinary category: a small change with security or data
integrity consequences belongs with `specialist`, even if it touches one file.
Use Luna only for clear, bounded tasks with explicit success criteria. Send
ambiguous or cross-module work to `specialist`. After an unsuccessful routine
attempt with an unclear cause, escalate with the evidence instead of repeating
the same approach. Have `implementer` apply a specialist's plan when execution
is routine; retain `specialist` for difficult implementation.

## Coordination

- State the selected role and a brief reason before starting substantive work.
- Spawn the custom agent by its role name so its model and reasoning settings
  apply. Use a fresh context when the spawn tool requires it for model changes;
  pass a self-contained brief with the objective, relevant evidence, constraints,
  owned files, acceptance criteria, and expected result.
- Keep at most three subagent threads open. Reuse appropriate existing agents
  for follow-ups and close completed threads when the runtime supports it.
- Parallelize independent searches and work on disjoint files. Assign each writer
  explicit paths. Never have two agents write the same file concurrently.
- Wait for prerequisite edits before running dependent tests or review. For
  substantial or sensitive behavior changes, use independent review. For exact
  low-impact edits, a diff check or existing focused validation is sufficient.
- Collect results, resolve findings, and finish the user's task in the main
  thread. Report concrete changes, checks, and remaining limitations. Do not
  claim a check passed unless it ran successfully.
- Preserve user changes and respect the current session's permissions. A role's
  instruction to avoid edits is behavioral guidance, not a separate sandbox.
- If a selected model is unavailable, report it and use `gpt-6.1-sol` at high
  effort if available, otherwise another account-supported model. Explicitly
  override the model for that fallback rather than retrying the unavailable
  custom role unchanged. If no permitted fallback exists, ask for a model choice.

Routing is instruction-based. User instructions and managed settings can override
it; this configuration does not implement a deterministic classification engine.
