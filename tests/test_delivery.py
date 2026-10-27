"""Shared cumulative-session delivery service (task 2.27; design sections 8.3, 10 and 14.1).

Sessions are built with the real controller and the fake adapter, then delivered through the
service and through `relay apply`, so the CLI and the service are held to the same prepared tree.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

from harness import command_line, git, init_repo
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.cli.commands import apply as apply_cli
from relay.cli.exitcodes import Exit
from relay.cli.main import main
from relay.core.apply import prepare_apply
from relay.core.checkpoints import record_integration
from relay.core.controller import SessionController
from relay.core.delivery import (
    ConfirmationInvalid,
    DeliveryBlocked,
    DeliveryPolicy,
    Verdict,
    accumulate_policy,
    derive_policy,
    install_delivery,
    prepare_checkpoint_delivery,
    prepare_run_delivery,
    refresh_delivery,
    verify_delivery,
)
from relay.core.events import read_events
from relay.core.registry import Config, validate_merged
from relay.core.sessions import SessionOwner, SessionStore
from relay.core.specs import Graph
from relay.core.store import RunStore

PASS = command_line([sys.executable, "-c", "pass"])


def config() -> Config:
    return validate_merged(
        {
            "version": 1,
            "defaults": {"concurrency": 3, "fallback": ["claude", "codex"]},
            "agents": {
                key: {"cmd": "fake", "concurrency": 3, "models": {"balanced": "default"}}
                for key in ("claude", "codex")
            },
        }
    )


def exists_check(path: str) -> str:
    code = f"import pathlib, sys; sys.exit(0 if pathlib.Path({path!r}).is_file() else 1)"
    return command_line([sys.executable, "-c", code])


def graph(
    nodes: list[dict[str, Any]],
    *,
    checks: tuple[str, ...] = (),
    verify: Literal["default", "none"] = "default",
) -> Graph:
    data: dict[str, Any] = {"policy": {"max_depth": 10}, "verify": verify}
    if verify == "none":
        data["verification_skip_reason"] = "delivery test"
    else:
        data["commands"] = {"test": PASS}
        data["integration_acceptance"] = [{"run": exists_check(path)} for path in checks]
    data["nodes"] = [{"category": "implement", "goal": "Implement", **n} for n in nodes]
    return Graph.model_validate(data)


def approve(
    owner: SessionOwner, spec: Graph, *, base: dict[str, Any] | None = None, key: str = "k"
) -> None:
    revision = owner.save_plan(
        spec.model_dump(mode="json", by_alias=True, exclude_unset=True),
        explanation="test",
        base=base,
    )
    owner.approve(
        revision,
        owner.store.plan(revision)["digest"],
        idempotency_key=key,
        expected_session_revision=owner.record.revision,
    )


def controller(owner: SessionOwner, adapter: FakeAdapter) -> SessionController:
    return SessionController(owner, config=config(), adapters={"claude": adapter, "codex": adapter})


async def until(ctl: SessionController, node: str, wanted: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    nodes: dict[str, dict[str, Any]] = ctl.snapshot()["nodes"]
    while nodes.get(node, {}).get("state") != wanted:
        nodes = ctl.snapshot()["nodes"]
        if loop.time() > deadline:
            raise AssertionError(f"node {node} never reached {wanted}")
        await asyncio.sleep(0.03)


@dataclass
class Built:
    repo: Path
    session_id: str
    runs: list[str]
    checkpoint_id: str | None = None

    def owner(self) -> SessionOwner:
        return SessionStore.open(self.repo, self.session_id).acquire_owner()

    @property
    def last(self) -> str:
        return self.runs[-1]


def build_integration_chain(tmp_path: Path, *, second_verify: str = "default") -> Built:
    """Round 1 integrates a/x.py; round 2 builds on that unapplied integration."""

    async def scenario() -> Built:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        sid = owner.store.session_id
        approve(owner, graph([{"id": "a", "owns": ["a/**"]}], checks=("a/x.py",)))
        ctl = controller(owner, FakeAdapter({"a": Scenario(write={"a/x.py": "a\n"})}))
        first = await ctl.start_round()
        assert (await ctl.finish_round()).exit_code == 0
        await ctl.shutdown()

        owner2 = SessionStore.open(repo, sid).acquire_owner()
        if second_verify == "none":
            second = graph([{"id": "e", "owns": ["a/**", "e/**"]}], verify="none")
            scenario2 = Scenario(write={"e/e.py": "e\n"}, delete=["a/x.py"])
        else:
            second = graph([{"id": "e", "owns": ["e/**"]}], checks=("e/e.py",))
            scenario2 = Scenario(write={"e/e.py": "e\n"})
        approve(owner2, second, base={"kind": "integration", "source_run": first}, key="k2")
        ctl2 = controller(owner2, FakeAdapter({"e": scenario2}))
        run2 = await ctl2.start_round()
        assert (await ctl2.finish_round()).exit_code == 0
        await ctl2.shutdown()
        return Built(repo, sid, [first, run2])

    return asyncio.run(scenario())


def build_checkpoint_chain(
    tmp_path: Path, *, evidence: Literal["none", "passed", "skipped"] = "none"
) -> Built:
    """Round 1 is revised into checkpoint c1; round 2 builds on it and finishes."""

    async def scenario() -> Built:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        sid = owner.store.session_id
        first = graph(
            [
                {"id": "a", "owns": ["a/**"]},
                {"id": "b", "owns": ["b/**"]},
            ],
            checks=("a/x.py",),
        )
        approve(owner, first)
        adapter = FakeAdapter(
            {
                "a": Scenario(write={"a/x.py": "a\n"}),
                "b": Scenario(hang=True, write={"b/partial.py": "unfinished\n"}),
            }
        )
        ctl = controller(owner, adapter)
        run1 = await ctl.start_round()
        await until(ctl, "a", "done")
        await until(ctl, "b", "running")
        manifest = await ctl.revise("rev", cancel_active=True, timeout_s=30)
        assert manifest is not None and manifest.state == "sealed"
        cid = manifest.checkpoint_id
        if evidence == "passed":
            record_integration(owner, cid, "passed", "ci green")
        elif evidence == "skipped":
            record_integration(owner, cid, "skipped", "operator accepts", operator_skip=True)

        follow = graph([{"id": "d", "owns": ["d/**"]}], verify="none")
        approve(owner, follow, base={"kind": "checkpoint", "checkpoint_id": cid}, key="k2")
        ctl2 = controller(owner, FakeAdapter({"d": Scenario(write={"d/d.py": "d\n"})}))
        run2 = await ctl2.start_round()
        assert (await ctl2.finish_round()).exit_code == 0
        await ctl2.shutdown()
        return Built(repo, sid, [run1, run2], cid)

    return asyncio.run(scenario())


def commands_run(verdict: Verdict) -> int:
    assert verdict.result is not None
    return len([c for c in verdict.result.checks if c.kind == "command"])


def tree_files(repo: Path, rev: str) -> set[str]:
    return set(git(repo, "ls-tree", "-r", "--name-only", rev).splitlines())


# --- policy accumulation -----------------------------------------------------------------------


def test_policy_only_accumulates_and_never_narrows() -> None:
    wide = graph([{"id": "a", "owns": ["a/**"]}], checks=("a/x.py", "b/y.py"))
    first = accumulate_policy(None, run_id="r1", graph=wide, baseline="b" * 40)
    assert first.write_scope == ["a/**"] and len(first.run_checks) == 2

    narrow = graph([{"id": "e", "owns": ["e/**"]}], checks=("a/x.py",))
    second = accumulate_policy(first, run_id="r2", graph=narrow, baseline="c" * 40)
    assert second.baseline_commit == "b" * 40  # the original baseline never moves
    assert second.write_scope == ["a/**", "e/**"]
    assert [c.run for c in second.run_checks] == [c.run for c in first.run_checks]
    assert len(second.run_checks) == 2  # a repeated check is not duplicated

    skipped = accumulate_policy(
        second,
        run_id="r3",
        graph=graph([{"id": "f", "owns": ["f/**"]}], verify="none"),
        baseline="d" * 40,
    )
    assert len(skipped.run_checks) == 2  # an opt-out removes nothing
    assert [s.run_id for s in skipped.skips] == ["r3"]
    assert skipped.rounds == ["r1", "r2", "r3"]
    assert set(first.write_scope) <= set(second.write_scope) <= set(skipped.write_scope)


def test_unscoped_write_node_makes_the_scope_unrestricted_and_scope_checks_conjoin() -> None:
    first = accumulate_policy(
        None,
        run_id="r1",
        graph=Graph.model_validate(
            {
                "commands": {"test": PASS},
                "integration_acceptance": [{"diff_scope": ["src/**"]}],
                "nodes": [{"id": "a", "category": "implement", "goal": "g", "owns": ["src/**"]}],
            }
        ),
        baseline="b" * 40,
    )
    second = accumulate_policy(
        first,
        run_id="r2",
        graph=Graph.model_validate(
            {
                "commands": {"test": PASS},
                "integration_acceptance": [{"diff_scope": ["docs/**"]}],
                "nodes": [{"id": "d", "category": "implement", "goal": "g"}],
            }
        ),
        baseline="c" * 40,
    )
    assert second.write_unrestricted
    assert second.scope_checks == [["src/**"], ["docs/**"]]  # every restriction must hold


def test_policy_is_recorded_when_a_round_finishes(tmp_path: Path) -> None:
    built = build_integration_chain(tmp_path)
    store = SessionStore.open(built.repo, built.session_id)
    recorded = DeliveryPolicy.model_validate(store.read().delivery_policy)
    assert recorded.rounds == built.runs
    assert recorded == derive_policy(built.repo, store)
    assert recorded.write_scope == ["a/**", "e/**"]
    assert len(recorded.run_checks) == 2


# --- the shared service ------------------------------------------------------------------------


def test_cumulative_delivery_verifies_every_round_and_installs_the_prepared_tree(
    tmp_path: Path,
) -> None:
    built = build_integration_chain(tmp_path)
    repo = built.repo
    prepared = prepare_run_delivery(repo, built.last)
    assert prepared.session_id == built.session_id and prepared.policy is not None
    assert [(c.kind, c.ref) for c in prepared.carried] == [("integration", built.runs[0])]
    assert set(prepared.cumulative_paths) == {"a/x.py", "e/e.py"}

    steps: list[str] = []
    verdict = verify_delivery(repo, prepared, on_step=steps.append)
    assert verdict.outcome == "passed" and commands_run(verdict) == 2  # both rounds' checks ran
    assert steps == ["verification-passed"]

    before = git(repo, "rev-parse", "main")
    seen: list[str] = []
    outcome = install_delivery(
        repo, prepared, confirmed_digest=prepared.digest, on_step=seen.append
    )
    assert seen == ["revalidated", "ref-updated", "installed"]
    assert outcome.result.changed and outcome.installed
    assert git(repo, "rev-parse", "main") != before
    assert {"a/x.py", "e/e.py"} <= tree_files(repo, "main")
    assert (repo / "a" / "x.py").is_file() and (repo / "e" / "e.py").is_file()


def test_a_narrower_later_round_cannot_replace_an_earlier_mandated_check(tmp_path: Path) -> None:
    # Round 2 opts out of command checks and removes a/x.py, which round 1 required.
    built = build_integration_chain(tmp_path, second_verify="none")
    prepared = prepare_run_delivery(built.repo, built.last)
    assert prepared.policy is not None and len(prepared.policy.run_checks) == 1
    assert [s.run_id for s in prepared.policy.skips] == [built.last]
    verdict = verify_delivery(built.repo, prepared)
    assert verdict.outcome == "failed" and commands_run(verdict) == 1


def test_delta_outside_the_cumulative_scope_fails_verification(tmp_path: Path) -> None:
    built = build_integration_chain(tmp_path)
    prepared = prepare_run_delivery(built.repo, built.last)
    assert prepared.policy is not None
    narrowed = prepared.policy.model_copy(update={"write_scope": ["a/**"]})
    verdict = verify_delivery(built.repo, dataclasses.replace(prepared, policy=narrowed))
    assert verdict.outcome == "failed" and "e/e.py" in verdict.detail
    restricted = prepared.policy.model_copy(update={"scope_checks": [["e/**"]]})
    verdict = verify_delivery(built.repo, dataclasses.replace(prepared, policy=restricted))
    assert verdict.outcome == "failed" and "a/x.py" in verdict.detail


@pytest.mark.parametrize("tamper", ["drop-check", "add-scope", "unknown-round"])
def test_an_edited_recorded_policy_blocks_delivery(tmp_path: Path, tamper: str) -> None:
    built = build_integration_chain(tmp_path)
    owner = built.owner()
    policy = dict(owner.record.delivery_policy or {})
    if tamper == "drop-check":
        policy["run_checks"] = policy["run_checks"][:1]
    elif tamper == "add-scope":
        policy["write_scope"] = [*policy["write_scope"], "elsewhere/**"]
    else:
        policy["rounds"] = ["2026-01-01-ffff", *policy["rounds"]]
    owner.set_delivery_policy(policy)
    owner.close()
    with pytest.raises(DeliveryBlocked, match="disagrees"):
        prepare_run_delivery(built.repo, built.last)


def test_a_moved_carried_integration_blocks_delivery(tmp_path: Path) -> None:
    built = build_integration_chain(tmp_path)
    git(
        built.repo,
        "update-ref",
        f"refs/heads/relay/{built.runs[0]}/integration",
        git(built.repo, "rev-parse", "main"),
    )
    with pytest.raises(DeliveryBlocked, match="moved"):
        prepare_run_delivery(built.repo, built.last)


def test_a_lost_node_output_ref_blocks_delivery(tmp_path: Path) -> None:
    built = build_integration_chain(tmp_path)
    git(built.repo, "update-ref", "-d", f"refs/relay/{built.runs[0]}/a")
    with pytest.raises(DeliveryBlocked, match="output ref"):
        prepare_run_delivery(built.repo, built.last)


def test_a_carried_checkpoint_must_be_deliverable(tmp_path: Path) -> None:
    built = build_checkpoint_chain(tmp_path)
    with pytest.raises(DeliveryBlocked, match="not deliverable"):
        prepare_run_delivery(built.repo, built.last)


@pytest.mark.parametrize("evidence", ["passed", "skipped"])
def test_a_deliverable_checkpoint_chain_applies_cumulatively(
    tmp_path: Path, evidence: Literal["passed", "skipped"]
) -> None:
    built = build_checkpoint_chain(tmp_path, evidence=evidence)
    prepared = prepare_run_delivery(built.repo, built.last)
    assert [(c.kind, c.ref) for c in prepared.carried] == [("checkpoint", built.checkpoint_id)]
    verdict = verify_delivery(built.repo, prepared)
    # Round 2 opted out of checks, but round 1's mandated check still runs on the full tree.
    assert verdict.outcome == "passed" and commands_run(verdict) == 1
    install_delivery(built.repo, prepared, confirmed_digest=prepared.digest)
    files = tree_files(built.repo, "main")
    assert {"a/x.py", "d/d.py"} <= files and "b/partial.py" not in files


def test_tampering_with_a_carried_checkpoint_blocks_delivery(tmp_path: Path) -> None:
    built = build_checkpoint_chain(tmp_path, evidence="passed")
    result = RunStore.open(built.repo, built.runs[0]).node_dir("a") / "result.spec.yaml"
    result.write_text(result.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    with pytest.raises(DeliveryBlocked, match="result changed"):
        prepare_run_delivery(built.repo, built.last)


def test_a_checkpoint_can_be_delivered_directly_once_deliverable(tmp_path: Path) -> None:
    built = build_checkpoint_chain(tmp_path)
    cid = built.checkpoint_id
    assert cid is not None
    with pytest.raises(DeliveryBlocked, match="not deliverable"):
        prepare_checkpoint_delivery(built.repo, built.session_id, cid)
    owner = built.owner()
    record_integration(owner, cid, "failed", "tests failed")
    owner.close()
    with pytest.raises(DeliveryBlocked, match="not deliverable"):
        prepare_checkpoint_delivery(built.repo, built.session_id, cid)
    owner = built.owner()
    record_integration(owner, cid, "skipped", "operator accepts", operator_skip=True)
    owner.close()

    prepared = prepare_checkpoint_delivery(built.repo, built.session_id, cid)
    assert prepared.request.kind == "checkpoint" and prepared.policy is not None
    assert verify_delivery(built.repo, prepared).outcome == "passed"
    install_delivery(built.repo, prepared, confirmed_digest=prepared.digest)
    files = tree_files(built.repo, "main")
    assert "a/x.py" in files and "d/d.py" not in files  # only the checkpoint, not round 2


def test_a_superseded_round_is_not_deliverable(tmp_path: Path) -> None:
    built = build_checkpoint_chain(tmp_path, evidence="passed")
    with pytest.raises(DeliveryBlocked, match="superseded"):
        prepare_run_delivery(built.repo, built.runs[0])


def test_a_conflicted_checkpoint_has_no_deliverable_base(tmp_path: Path) -> None:
    async def scenario() -> Built:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, graph([{"id": "a"}, {"id": "b"}], verify="none"))
        adapter = FakeAdapter(
            {
                "a": Scenario(write={"shared.txt": "from a\n"}),
                "b": Scenario(write={"shared.txt": "from b\n"}),
            }
        )
        ctl = controller(owner, adapter)
        run_id = await ctl.start_round()
        await until(ctl, "a", "done")
        await until(ctl, "b", "done")
        manifest = await ctl.revise("rev", timeout_s=30)
        assert manifest is not None and manifest.state == "conflicted"
        await ctl.shutdown()
        return Built(repo, owner.store.session_id, [run_id], manifest.checkpoint_id)

    built = asyncio.run(scenario())
    assert built.checkpoint_id is not None
    with pytest.raises(DeliveryBlocked, match="conflicted"):
        prepare_checkpoint_delivery(built.repo, built.session_id, built.checkpoint_id)


# --- confirmation binds to the prepared operation ----------------------------------------------


def test_confirmation_is_bound_to_the_prepared_digest(tmp_path: Path) -> None:
    built = build_integration_chain(tmp_path)
    prepared = prepare_run_delivery(built.repo, built.last)
    before = git(built.repo, "rev-parse", "main")
    with pytest.raises(ConfirmationInvalid, match="different"):
        install_delivery(built.repo, prepared, confirmed_digest="0" * 64)
    assert git(built.repo, "rev-parse", "main") == before
    # Preparing the same operation twice yields the same digest.
    assert refresh_delivery(built.repo, prepared).digest == prepared.digest


def test_a_moved_target_invalidates_the_confirmation(tmp_path: Path) -> None:
    built = build_integration_chain(tmp_path)
    prepared = prepare_run_delivery(built.repo, built.last)
    (built.repo / "later.txt").write_text("later\n", encoding="utf-8")
    git(built.repo, "add", "later.txt")
    git(built.repo, "commit", "-q", "-m", "later")
    moved = git(built.repo, "rev-parse", "main")
    with pytest.raises(ConfirmationInvalid, match="changed while confirming"):
        install_delivery(built.repo, prepared, confirmed_digest=prepared.digest)
    assert git(built.repo, "rev-parse", "main") == moved


def test_a_session_revision_change_invalidates_the_confirmation(tmp_path: Path) -> None:
    built = build_integration_chain(tmp_path)
    prepared = prepare_run_delivery(built.repo, built.last)
    before = git(built.repo, "rev-parse", "main")
    owner = built.owner()  # any durable session change, e.g. a further approval or policy edit
    owner.mark_active()
    owner.close()
    with pytest.raises(ConfirmationInvalid):
        install_delivery(built.repo, prepared, confirmed_digest=prepared.digest)
    assert git(built.repo, "rev-parse", "main") == before


def test_new_checkpoint_evidence_invalidates_the_confirmation(tmp_path: Path) -> None:
    built = build_checkpoint_chain(tmp_path, evidence="passed")
    prepared = prepare_run_delivery(built.repo, built.last)
    owner = built.owner()
    assert built.checkpoint_id is not None
    record_integration(owner, built.checkpoint_id, "skipped", "changed my mind", operator_skip=True)
    owner.close()
    with pytest.raises(ConfirmationInvalid):
        install_delivery(built.repo, prepared, confirmed_digest=prepared.digest)


# --- core refactor -----------------------------------------------------------------------------


def test_prepare_apply_accepts_an_explicit_source_commit(tmp_path: Path) -> None:
    built = build_integration_chain(tmp_path)
    first = RunStore.open(built.repo, built.runs[0]).read_run().final_commit
    assert first is not None
    plan = prepare_apply(built.repo, built.last, source_commit=first)
    assert plan.integration_commit == first
    assert plan.changed_paths == ("a/x.py",)


# --- relay apply <run> resolves the session policy ---------------------------------------------


def invoke(repo: Path, run_id: str, *args: str) -> int:
    return main(["-C", str(repo), "apply", run_id, *args])


def test_apply_cli_delivers_the_cumulative_session_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built = build_integration_chain(tmp_path)
    assert invoke(built.repo, built.last, "--yes", "--json") == int(Exit.OK)
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == built.session_id
    assert payload["verification"] == {"outcome": "passed", "checks": 4}
    assert [item["ref"] for item in payload["carried"]] == [built.runs[0]]
    assert len(payload["delivery_digest"]) == 64
    assert {"a/x.py", "e/e.py"} <= tree_files(built.repo, "main")
    store = RunStore.open(built.repo, built.last)
    steps = [
        event.data.get("step")
        for event in read_events(store.events_path)
        if event.type == "apply.progress"
    ]
    assert steps == ["prepared", "verification-passed", "confirmed", "ref-updated", "installed"]


def test_apply_cli_cannot_bypass_the_session_policy_by_naming_the_last_round(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built = build_integration_chain(tmp_path, second_verify="none")
    before = git(built.repo, "rev-parse", "main")
    assert invoke(built.repo, built.last, "--yes") == int(Exit.VERIFICATION_FAILED)
    assert git(built.repo, "rev-parse", "main") == before
    assert "verification failed" in capsys.readouterr().err


def test_apply_cli_refuses_a_blocked_session_delivery_and_lists_the_problems(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built = build_checkpoint_chain(tmp_path)  # the carried checkpoint has no evidence
    before = git(built.repo, "rev-parse", "main")
    assert invoke(built.repo, built.last, "--yes") == int(Exit.FAILURE)
    err = capsys.readouterr().err
    assert "delivery is blocked" in err and "not deliverable" in err
    assert git(built.repo, "rev-parse", "main") == before


def test_apply_cli_without_confirmation_exits_approval_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built = build_integration_chain(tmp_path)
    monkeypatch.setattr(apply_cli, "_interactive", lambda: False)
    before = git(built.repo, "rev-parse", "main")
    assert invoke(built.repo, built.last) == int(Exit.APPROVAL_REQUIRED)
    assert git(built.repo, "rev-parse", "main") == before
    assert "relay apply" in capsys.readouterr().err


def test_apply_cli_target_moved_while_confirming_invalidates_the_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built = build_integration_chain(tmp_path)
    monkeypatch.setattr(apply_cli, "_interactive", lambda: True)

    def move_then_confirm(*_args: object) -> str:
        (built.repo / "later.txt").write_text("later\n", encoding="utf-8")
        git(built.repo, "add", "later.txt")
        git(built.repo, "commit", "-q", "-m", "later")
        return "y"

    monkeypatch.setattr("builtins.input", move_then_confirm)
    assert invoke(built.repo, built.last) == int(Exit.APPROVAL_REQUIRED)
    assert "no longer applies" in capsys.readouterr().err
    assert "a/x.py" not in tree_files(built.repo, "main")


def test_apply_cli_is_blocked_while_another_process_owns_the_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built = build_integration_chain(tmp_path)
    owner = built.owner()
    try:
        assert invoke(built.repo, built.last, "--yes") == int(Exit.FAILURE)
    finally:
        owner.close()
    assert "owned by another process" in capsys.readouterr().err
