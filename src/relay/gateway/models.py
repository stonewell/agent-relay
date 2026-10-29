"""Pinned public model ids derived only from qualified completion backends.

Each qualified agent offers ``<agent>/<alias>`` for every configured CLI model alias, plus the
bare ``<agent>`` id for its first alias. Advertised limits come only from discovery
(``relay.gateway.discovery``): an alias whose limits were not observed is not listed.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from relay.adapters.completion import (
    CompletionCapable,
    CompletionError,
    CompletionRequest,
    CompletionSupport,
    CompletionTerminationError,
    ObservedModel,
    model_aliases,
)
from relay.core.classifier import classify
from relay.core.registry import Config
from relay.core.router import score_candidate

from .discovery import PROBE_OUTPUT_BYTES, PROBE_PROMPT, PROBE_TIMEOUT_S, ModelCache, discover
from .protocol import ChatRequest, GatewayError
from .runtime import GatewayRuntime

ModelState = Literal["observed", "undiscovered", "failed"]


@dataclass(frozen=True)
class GatewayModel:
    id: str
    agent: str
    model: str
    effort: str | None
    support: CompletionSupport
    adapter: CompletionCapable
    cost: str | None = None

    def public(self, observed: ObservedModel) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": "relay",
            "relay": {
                "resolved_model": f"{self.agent}/{observed.model}",
                "model_alias": self.model,
                "supports_tools": self.support.supports_tools,
                "native_max_tokens": self.support.native_max_tokens,
                "ignored_parameters": ["temperature", "top_p"],
                "context_length": observed.context_length,
                "max_output_tokens": observed.max_output_tokens,
                "cost_class": self.cost,
            },
        }

    def check(self, request: ChatRequest, observed: ObservedModel | None) -> None:
        if request.tools and not self.support.supports_tools:
            raise GatewayError(400, "model does not support tools", "tools")
        if request.output_tokens is not None and not self.support.native_max_tokens:
            param = (
                "max_completion_tokens"
                if request.max_completion_tokens is not None
                else "max_tokens"
            )
            raise GatewayError(400, "model does not enforce native output token limits", param)
        if (
            observed is not None
            and request.output_tokens is not None
            and request.output_tokens > observed.max_output_tokens
        ):
            param = (
                "max_completion_tokens"
                if request.max_completion_tokens is not None
                else "max_tokens"
            )
            raise GatewayError(400, "requested output exceeds observed model limit", param)


class ModelCatalog:
    def __init__(
        self,
        config: Config,
        adapters: Mapping[str, CompletionCapable],
        models: Sequence[str] | None = None,
        cache: ModelCache | None = None,
    ) -> None:
        self.config = config
        self.aliases = model_aliases(models)
        self.cache = cache or ModelCache(None)
        self.models: dict[str, GatewayModel] = {}
        self.defaults: list[GatewayModel] = []
        self._supports: dict[str, tuple[CompletionCapable, CompletionSupport]] = {}
        # Replaced wholesale (never mutated in place) so event-loop readers see a snapshot.
        self._observed: dict[tuple[str, str], ObservedModel] = {}
        self._failed: frozenset[tuple[str, str]] = frozenset()
        self._attempted: set[tuple[str, str]] = set()
        self._task: asyncio.Task[None] | None = None
        self._cancel = threading.Event()
        for name, agent in config.agents.items():
            if name == "relay" or "/" in name:
                raise ValueError("gateway agent ids must not contain slashes or reserve relay")
            if not agent.enabled or name not in adapters:
                continue
            adapter = adapters[name]
            support = adapter.inspect()
            if support is None:
                continue
            self._supports[name] = (adapter, support)
            alias = GatewayModel(name, name, self.aliases[0], None, support, adapter, agent.cost)
            self._add(alias)
            self.defaults.append(alias)
            for model in self.aliases:
                self._add(
                    GatewayModel(f"{name}/{model}", name, model, None, support, adapter, agent.cost)
                )

    def _add(self, model: GatewayModel) -> None:
        if model.id in self.models or model.id == "relay/auto":
            raise ValueError(f"gateway model id collision: {model.id}")
        self.models[model.id] = model

    # Discovery -----------------------------------------------------------------------------

    def observe(
        self,
        *,
        probe: bool,
        refresh: bool = False,
        timeout_s: float = PROBE_TIMEOUT_S,
        probe_model: Callable[[CompletionCapable, str], ObservedModel | None] | None = None,
    ) -> None:
        """Load cached observations and, when ``probe``, probe aliases not yet tried.

        Blocking; the gateway runs it in a worker thread, one discovery at a time. Raises
        ``CompletionTerminationError`` when a probe's termination cannot be confirmed.
        """
        observed = dict(self._observed)
        failed = set(self._failed)
        try:
            for agent, (adapter, support) in self._supports.items():
                skip = frozenset(
                    alias for alias in self.aliases if (agent, alias) in self._attempted
                )
                result = discover(
                    adapter,
                    support.profile,
                    self.aliases,
                    self.cache,
                    probe_missing=probe,
                    refresh=refresh,
                    skip=frozenset() if refresh else skip,
                    cancel=self._cancel,
                    timeout_s=timeout_s,
                    probe_model=probe_model,
                )
                self._attempted.update((agent, alias) for alias in result.probed)
                for alias in self.aliases:
                    if alias in result.observed:
                        observed[(agent, alias)] = result.observed[alias]
                        failed.discard((agent, alias))
                    elif alias in result.failed:
                        observed.pop((agent, alias), None)
                        failed.add((agent, alias))
        finally:
            self._observed, self._failed = observed, frozenset(failed)

    async def discover(self, runtime: GatewayRuntime) -> None:
        """Single-flight lazy discovery; concurrent callers share one in-progress probe run."""
        if self._task is None or self._task.done():
            loop = asyncio.get_running_loop()

            async def run_probe(adapter: CompletionCapable, alias: str) -> ObservedModel | None:
                try:
                    result = await runtime.complete_when_available(
                        adapter,
                        CompletionRequest(PROBE_PROMPT, alias),
                        cancel=self._cancel,
                        timeout_s=PROBE_TIMEOUT_S,
                        max_output_bytes=PROBE_OUTPUT_BYTES,
                    )
                except CompletionTerminationError:
                    raise
                except CompletionError as exc:
                    if exc.code in {"unavailable", "request_cancelled"}:
                        raise
                    return None
                except OSError:
                    return None
                return result.observed

            def probe_model(adapter: CompletionCapable, alias: str) -> ObservedModel | None:
                return asyncio.run_coroutine_threadsafe(run_probe(adapter, alias), loop).result()

            async def run() -> None:
                await asyncio.to_thread(self.observe, probe=True, probe_model=probe_model)

            self._task = asyncio.create_task(run())
            self._task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        # A disconnecting client must not cancel discovery that other callers await.
        await asyncio.shield(self._task)

    async def close(self) -> None:
        """Cancel and drain an in-progress probe."""
        self._cancel.set()
        task = self._task
        while task is not None and not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break

    def state(self, model_id: str) -> ModelState:
        if model_id == "relay/auto":
            states = [self.state(model.id) for model in self.defaults]
            if all(state == "observed" for state in states) and states:
                return "observed"
            return "undiscovered" if "undiscovered" in states else "failed"
        model = self.models[model_id]
        key = (model.agent, model.model)
        if key in self._observed:
            return "observed"
        return "failed" if key in self._failed else "undiscovered"

    # Listing and routing --------------------------------------------------------------------

    def _public(
        self, model_id: str, observed: Mapping[tuple[str, str], ObservedModel]
    ) -> dict[str, Any] | None:
        if model_id == "relay/auto":
            known = [observed.get((model.agent, model.model)) for model in self.defaults]
            limits = [entry for entry in known if entry is not None]
            if not limits or len(limits) != len(known):
                return None
            return {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "relay",
                "relay": {
                    "resolved_model": "auto",
                    "supports_tools": all(m.support.supports_tools for m in self.defaults),
                    "native_max_tokens": all(m.support.native_max_tokens for m in self.defaults),
                    "ignored_parameters": ["temperature", "top_p"],
                    "context_length": min(entry.context_length for entry in limits),
                    "max_output_tokens": min(entry.max_output_tokens for entry in limits),
                },
            }
        model = self.models.get(model_id)
        if model is None:
            return None
        entry = observed.get((model.agent, model.model))
        return None if entry is None else model.public(entry)

    def public(self, model_id: str) -> dict[str, Any]:
        value = self._public(model_id, self._observed)
        if value is None:
            raise GatewayError(404, "unknown model", "model", "model_not_found")
        return value

    def listing(self) -> dict[str, Any]:
        """Only models with observed limits; undiscovered or failed aliases are omitted."""
        observed = self._observed
        ids = [*self.models, *(["relay/auto"] if self.defaults else [])]
        entries = [self._public(model_id, observed) for model_id in ids]
        return {"object": "list", "data": [entry for entry in entries if entry is not None]}

    def candidates(self, request: ChatRequest) -> list[GatewayModel]:
        observed = self._observed
        if request.model != "relay/auto":
            # Configured aliases are routable before (or without) discovery; discovery only
            # decides what is advertised with limits.
            model = self.models.get(request.model)
            if model is None:
                raise GatewayError(404, "unknown model", "model", "model_not_found")
            model.check(request, observed.get((model.agent, model.model)))
            return [model]
        if not self.defaults:
            raise GatewayError(
                503, "no qualified completion backend available", "model", "model_unavailable"
            )
        candidates: list[GatewayModel] = []
        error: GatewayError | None = None
        for model in self.defaults:
            try:
                model.check(request, observed.get((model.agent, model.model)))
                candidates.append(model)
            except GatewayError as exc:
                error = exc
        if not candidates:
            assert error is not None
            raise error
        text = next(
            (
                message.text() or ""
                for message in reversed(request.messages)
                if message.role == "user"
            ),
            "",
        )
        classification = classify(text)
        order = {name: index for index, name in enumerate(self.config.defaults.fallback)}
        candidates.sort(
            key=lambda model: (
                -score_candidate(
                    model.agent, self.config.agents[model.agent], classification, {}, {}
                ),
                order.get(model.agent, len(order)),
            )
        )
        return candidates
