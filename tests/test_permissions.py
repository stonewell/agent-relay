"""Permission levels: precedence, fail-closed checks and the run-start banner (task 1.20 part A).

Pure unit tests: no network, no real CLIs, no availability probing.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harness import context, task
from relay.adapters.claude import ClaudeAdapter
from relay.adapters.claude import PermissionUnsupported as ClaudeUnsupported
from relay.adapters.codex import CodexAdapter
from relay.core.permissions import (
    DEFAULT_LEVEL,
    LEVELS,
    PermissionDecision,
    UnknownPermissionLevel,
    UnsupportedPermissionLevel,
    decide,
    format_banner,
    normalize_level,
    resolve_level,
)
from relay.core.registry import Agent
from relay.core.specs.common import PermissionLevel

# --- canonical set and normalization ---------------------------------------------------------


def test_canonical_level_set() -> None:
    assert LEVELS == ("full", "workspace", "readonly")
    assert DEFAULT_LEVEL == "full"


@pytest.mark.parametrize("level", ["full", "workspace", "readonly"])
def test_normalize_accepts_canonical_levels(level: str) -> None:
    assert normalize_level(level) == level


@pytest.mark.parametrize("bad", ["root", "bypassPermissions", "FULL", "workspace ", "", None, 0])
def test_normalize_rejects_unknown_values(bad: object) -> None:
    with pytest.raises(UnknownPermissionLevel):
        normalize_level(bad)


def test_normalize_rejects_unhashable_values() -> None:
    with pytest.raises(UnknownPermissionLevel):
        normalize_level(["full"])  # type: ignore[arg-type]


# --- precedence resolution (design section 9.2) ---------------------------------------------


def test_default_is_full_when_nothing_is_set() -> None:
    resolution = resolve_level()
    assert resolution.requested is None
    assert resolution.effective == "full"
    assert resolution.source == "default"


def test_explicit_task_request_over_configuration_default() -> None:
    resolution = resolve_level(task="readonly", default="full")
    assert resolution.requested == "readonly"
    assert resolution.effective == "readonly"
    assert resolution.source == "task"


def test_cli_request_is_most_restrictive_of_cli_and_task() -> None:
    resolution = resolve_level(task="workspace", cli="readonly")
    assert resolution.requested == "readonly"
    assert resolution.effective == "readonly"


def test_agent_config_restricts_the_default() -> None:
    resolution = resolve_level(agent="workspace", default="full")
    assert resolution.requested is None
    assert resolution.effective == "workspace"
    assert resolution.source == "agent"


def test_planner_read_only_imposes_readonly_even_at_full_default() -> None:
    resolution = resolve_level(default="full", planner_read_only=True)
    assert resolution.effective == "readonly"
    assert resolution.source == "planner"


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (dict(task="full", default="full"), "full"),
        (dict(task="full", default="workspace"), "workspace"),
        (dict(cli="full", task="readonly", default="full"), "readonly"),
        (dict(cli="workspace", agent="readonly", default="full"), "readonly"),
        (dict(plugin="workspace", default="full"), "workspace"),
    ],
)
def test_precedence_matrix_is_cumulative(
    sources: dict[str, PermissionLevel], expected: PermissionLevel
) -> None:
    assert (
        resolve_level(
            task=sources.get("task"),
            cli=sources.get("cli"),
            plugin=sources.get("plugin"),
            agent=sources.get("agent"),
            default=sources.get("default", DEFAULT_LEVEL),
        ).effective
        == expected
    )


def test_source_order_never_widens_an_explicit_restriction() -> None:
    # A run default of `readonly` is never widened by a task asking for `full`.
    resolution = resolve_level(task="full", default="readonly")
    assert resolution.requested == "full"
    assert resolution.effective == "readonly"
    assert resolution.source == "default"


def test_requested_is_none_when_only_configuration_restricts() -> None:
    resolution = resolve_level(agent="workspace", default="readonly")
    assert resolution.requested is None
    assert resolution.effective == "readonly"


# --- fail-closed decision (design section 9.3) ----------------------------------------------


@pytest.mark.parametrize("adapter", ["claude", "codex", "agy"])
def test_unsupported_level_fails_closed_per_adapter(adapter: str) -> None:
    # Each real adapter advertises `full` only until the profiles pass doctor canaries.
    resolution = resolve_level(task="readonly")
    decision = decide(adapter, ("full",), resolution)
    assert decision.supported is False
    assert decision.effective == "readonly"
    with pytest.raises(UnsupportedPermissionLevel, match="cannot honour"):
        decision.require_supported()


def test_fake_adapter_supports_every_level() -> None:
    advertised = ("full", "workspace", "readonly")
    for level in advertised:
        decision = decide("fake", advertised, resolve_level(task=level))
        assert decision.supported is True
        assert decision.effective == level
        decision.require_supported()  # does not raise


def test_unsupported_never_silently_widens_to_full() -> None:
    decision = decide("claude", ("full",), resolve_level(task="workspace"))
    assert decision.effective == "workspace"  # unchanged, not widened to `full`
    assert decision.supported is False


def test_supported_decision_carries_adapter_support() -> None:
    decision = decide("codex", ("full",), resolve_level())
    assert decision.adapter == "codex"
    assert decision.advertised == ("full",)
    assert decision.supported is True
    assert decision.reason is None


def test_decision_reports_reason_for_unsupported() -> None:
    decision = decide("claude", ("full",), resolve_level(task="readonly"))
    assert decision.reason is not None
    assert "readonly" in decision.reason and "full" in decision.reason


def test_unknown_advertised_level_returns_unsupported_instead_of_raising() -> None:
    # A bogus *advertised* level is not a caller input error: `decide` never raises and
    # reports the adapter as unsupported (F3). Unknown *requested* levels still raise.
    decision = decide("claude", ("full", "bogus"), resolve_level(task="full"))  # type: ignore[arg-type]
    assert decision.supported is False
    assert decision.reason is not None
    assert "bogus" in decision.reason
    assert decision.advertised == ("full",)


def test_decision_is_a_frozen_dataclass_with_expected_shape() -> None:
    decision = decide("claude", ("full",), resolve_level(task="readonly"))
    assert isinstance(decision, PermissionDecision)
    assert isinstance(decision.requested, (str, type(None)))
    assert decision.requested == "readonly"
    assert decision.effective == "readonly"
    assert decision.source == "task"


# --- permission mapping keeps the restricted flags -------------------------------------------


def _claude_agent() -> Agent:
    return Agent(cmd="claude", env={})


def test_codex_mapping_keeps_restricted_flags(
    tmp_path: Path,
) -> None:
    # The mapping still expresses every level (section 7.2) with unchanged flags.
    adapter = CodexAdapter(levels=("full", "workspace", "readonly"))
    workspace = adapter.build_invocation(task(), replace(context(tmp_path), permission="workspace"))
    assert "-s" in workspace.args and "workspace-write" in workspace.args
    readonly = adapter.build_invocation(task(), replace(context(tmp_path), permission="readonly"))
    assert "-s" in readonly.args and "read-only" in readonly.args


def test_claude_mapping_keeps_readonly(tmp_path: Path) -> None:
    # `workspace` still fails closed in the mapping itself.
    adapter = ClaudeAdapter(_claude_agent(), parent_env={})
    full = adapter.build_invocation(task(), replace(context(tmp_path), permission="full"))
    assert "--dangerously-skip-permissions" in full.args
    readonly = adapter.build_invocation(task(), replace(context(tmp_path), permission="readonly"))
    assert "--permission-mode" in readonly.args and "plan" in readonly.args
    with pytest.raises(ClaudeUnsupported):
        adapter.build_invocation(task(), replace(context(tmp_path), permission="workspace"))


# --- banner (design sections 9.4, 14) -------------------------------------------------------


def test_banner_states_active_level_per_agent() -> None:
    decisions = [
        decide("claude", ("full",), resolve_level()),
        decide("codex", ("full", "workspace", "readonly"), resolve_level(task="readonly")),
    ]
    banner = format_banner(decisions, run_id="r1")
    assert "permissions for run r1" in banner
    assert "claude: full" in banner
    assert "codex: readonly" in banner
    assert "warning:" in banner  # claude runs at full


def test_banner_warns_when_full_is_active() -> None:
    banner = format_banner([decide("claude", ("full",), resolve_level())])
    assert "warning: claude runs at full permission" in banner


def test_banner_has_no_full_warning_for_restricted_level() -> None:
    banner = format_banner(
        [decide("claude", ("full", "workspace", "readonly"), resolve_level(task="readonly"))]
    )
    assert "warning:" not in banner
    assert "claude: readonly" in banner


def test_banner_reports_a_rejected_level() -> None:
    decision = decide("claude", ("full",), resolve_level(task="readonly"))
    banner = format_banner([decision])
    assert "claude: readonly REJECTED" in banner
    assert "warning:" in banner
    assert "fail closed" in banner


def test_banner_marks_requested_level_when_it_differs_from_effective() -> None:
    decision = decide(
        "fake", ("full", "workspace", "readonly"), resolve_level(task="full", default="workspace")
    )
    banner = format_banner([decision])
    # effective is `workspace` (default), but the task requested `full`; both are shown.
    assert "fake: workspace" in banner
    assert "(requested full)" in banner
