"""Immutable graph input snapshots and retained merge ancestry (section 8.3)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from . import publish as publication
from .gitbase import read_ref


def retain(repo: Path, ref: str, commit: str) -> None:
    """Keep each immutable merge input alive until run purge."""
    prior = read_ref(repo, ref)
    if prior == commit:
        return
    if prior is not None:
        raise publication.PublicationError(f"immutable snapshot ref changed: {ref}")
    if not publication._compare_and_swap(repo, ref, commit, "0" * len(commit)):  # pyright: ignore[reportPrivateUsage]
        raise publication.PublicationError(f"snapshot ref raced: {ref}")


def merge_snapshot(
    repo: Path,
    run_id: str,
    consumer: str,
    base: str,
    outputs: Sequence[tuple[str, str]],
    *,
    verify: Callable[[str], None],
    resolve: Callable[[str, str, str], str] | None = None,
) -> str:
    """Merge in caller-provided topological order, verify each merge, retain its parents."""
    # A downstream output already includes its ancestors, including any verified conflict
    # resolution. Re-merging the original conflicting siblings would undo that resolution.
    outputs = [
        (key, commit)
        for index, (key, commit) in enumerate(outputs)
        if not any(
            index != other_index
            and (commit != other or index < other_index)
            and publication._is_ancestor(repo, commit, other)  # pyright: ignore[reportPrivateUsage]
            for other_index, (_, other) in enumerate(outputs)
        )
    ]
    current = base
    for index, (node_id, incoming) in enumerate(outputs):
        try:
            merged, created = publication._merge_into(  # pyright: ignore[reportPrivateUsage]
                repo, current, incoming, node_id, run_id, None
            )
        except publication.IntegrationConflictError:
            if resolve is None:
                raise
            merged = resolve(current, incoming, f"{consumer}-{index}")
            created = True
        if created:
            verify(merged)
        # Include the input commit in the namespace so invalidation never overwrites history.
        retain(repo, f"refs/relay/{run_id}/snapshots@{consumer}/{index}-{merged}", merged)
        current = merged
    return current
