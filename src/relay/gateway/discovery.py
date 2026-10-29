"""Discover served models and their limits from the CLI itself, cached per qualified profile.

The Claude CLI has no model-listing command. Each configured alias is therefore probed once
with a minimal prompt through the backend's ordinary ``complete()`` path, which re-checks
qualification and runs in a fresh scratch directory with the isolated completion
environment. The CLI's ``modelUsage`` report names the model that actually ran and its
context window and output limit. A probe that fails or reports no unambiguous limits leaves
that alias undiscovered: limits are observed, never recorded in qualification or invented.

Observations are cached on disk keyed by the full completion profile (CLI version, platform,
authentication mode and configuration digest), so a CLI upgrade or configuration change is a
cache miss. Importing this module does not import the optional HTTP stack.
"""

from __future__ import annotations

import json
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from relay.adapters.completion import (
    CompletionCapable,
    CompletionError,
    CompletionProfile,
    CompletionRequest,
    CompletionTerminationError,
    ObservedModel,
)
from relay.core.registry import USER_CONFIG, Config
from relay.core.store import atomic_write, read_text

PROBE_PROMPT = "Reply with the single word OK."
PROBE_TIMEOUT_S = 120.0
PROBE_OUTPUT_BYTES = 1024 * 1024
CACHE_SCHEMA = 1
CACHE_LIMIT = 1024 * 1024


def default_cache_path() -> Path:
    """Beside the user-level relay configuration; disposable and rebuilt by probing."""
    return USER_CONFIG.parent / "gateway-models.json"


def default_adapters(config: Config) -> dict[str, CompletionCapable]:
    from relay.adapters.claude_completion import ClaudeCompletionAdapter

    return (
        {"claude": ClaudeCompletionAdapter(config.agents["claude"])}
        if "claude" in config.agents
        else {}
    )


def _profile_key(profile: CompletionProfile) -> dict[str, str]:
    return {key: str(value) for key, value in asdict(profile).items()}


def _model(raw: object) -> ObservedModel | None:
    if not isinstance(raw, dict):
        return None
    entry = cast("dict[str, Any]", raw)
    model, context, output = (
        entry.get("model"),
        entry.get("context_length"),
        entry.get("max_output_tokens"),
    )
    if (
        not isinstance(model, str)
        or not model
        or type(context) is not int
        or type(output) is not int
        or context <= 0
        or output <= 0
    ):
        return None
    return ObservedModel(model, context, output)


class ModelCache:
    """One JSON file of observations per profile. ``path=None`` keeps nothing on disk.

    Missing, oversized or corrupt files and malformed entries read as a cache miss; a
    failed write is reported by ``store`` and otherwise ignored. Writes are atomic.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _entries(self) -> list[dict[str, Any]]:
        if self.path is None:
            return []
        try:
            if self.path.stat().st_size > CACHE_LIMIT:
                return []
            parsed: Any = json.loads(read_text(self.path))
        except (OSError, ValueError, UnicodeError, RecursionError):
            return []
        if not isinstance(parsed, dict):
            return []
        document = cast("dict[str, Any]", parsed)
        entries = document.get("profiles")
        if document.get("schema") != CACHE_SCHEMA or not isinstance(entries, list):
            return []
        return [
            cast("dict[str, Any]", entry)
            for entry in cast("list[object]", entries)
            if isinstance(entry, dict)
        ]

    def load(self, profile: CompletionProfile) -> dict[str, ObservedModel]:
        key = _profile_key(profile)
        with self._lock:
            entries = self._entries()
        for entry in entries:
            models = entry.get("models")
            if entry.get("profile") != key or not isinstance(models, dict):
                continue
            observed: dict[str, ObservedModel] = {}
            for alias, raw in cast("dict[str, object]", models).items():
                model = _model(raw)
                if model is not None:
                    observed[alias] = model
            return observed
        return {}

    def store(self, profile: CompletionProfile, models: Mapping[str, ObservedModel]) -> bool:
        """Replace this agent's entry; entries of older profiles of the same agent are dropped."""
        if self.path is None:
            return True
        key = _profile_key(profile)
        with self._lock:
            kept = [
                entry
                for entry in self._entries()
                if not isinstance(entry.get("profile"), dict)
                or cast("dict[str, object]", entry["profile"]).get("agent") != profile.agent
            ]
            kept.append(
                {"profile": key, "models": {alias: asdict(m) for alias, m in models.items()}}
            )
            document = {"schema": CACHE_SCHEMA, "profiles": kept}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(self.path, json.dumps(document, indent=2, sort_keys=True) + "\n")
            except OSError:
                return False
        return True


def probe(
    adapter: CompletionCapable,
    alias: str,
    *,
    cancel: threading.Event,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> ObservedModel | None:
    """One minimal qualified completion; ``None`` unless the CLI reported usable limits.

    Termination that cannot be confirmed is re-raised so the caller can quarantine.
    """
    try:
        with tempfile.TemporaryDirectory(
            prefix="relay-model-probe-", ignore_cleanup_errors=True
        ) as scratch:
            result = adapter.complete(
                CompletionRequest(PROBE_PROMPT, alias),
                cwd=Path(scratch),
                cancel=cancel,
                timeout_s=timeout_s,
                max_output_bytes=PROBE_OUTPUT_BYTES,
            )
    except CompletionTerminationError:
        raise
    except (CompletionError, OSError):
        return None
    return result.observed


@dataclass
class Discovery:
    observed: dict[str, ObservedModel] = field(default_factory=lambda: {})
    probed: list[str] = field(default_factory=lambda: [])
    failed: list[str] = field(default_factory=lambda: [])
    undiscovered: list[str] = field(default_factory=lambda: [])
    stored: bool = True


def discover(
    adapter: CompletionCapable,
    profile: CompletionProfile,
    aliases: Iterable[str],
    cache: ModelCache,
    *,
    probe_missing: bool,
    refresh: bool = False,
    skip: frozenset[str] = frozenset(),
    cancel: threading.Event | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
    probe_model: Callable[[CompletionCapable, str], ObservedModel | None] | None = None,
) -> Discovery:
    """Cached observations, probing uncached aliases when ``probe_missing``.

    ``refresh`` ignores the cache and re-probes every alias. Aliases in ``skip`` (already
    tried by this caller) are never probed again. Successful probes are written back even if
    a later probe raises.
    """
    cancel = cancel or threading.Event()
    result = Discovery()
    cached = {} if refresh else cache.load(profile)
    try:
        for alias in aliases:
            if alias in cached:
                result.observed[alias] = cached[alias]
            elif not probe_missing or alias in skip or cancel.is_set():
                result.undiscovered.append(alias)
            else:
                result.probed.append(alias)
                observed = (
                    probe_model(adapter, alias)
                    if probe_model is not None
                    else probe(adapter, alias, cancel=cancel, timeout_s=timeout_s)
                )
                if observed is None:
                    result.failed.append(alias)
                else:
                    result.observed[alias] = observed
    finally:
        if result.probed:
            merged = cache.load(profile)
            merged.update(
                {
                    alias: result.observed[alias]
                    for alias in result.probed
                    if alias in result.observed
                }
            )
            if refresh:
                for alias in result.failed:
                    merged.pop(alias, None)
            result.stored = cache.store(profile, merged)
    return result
