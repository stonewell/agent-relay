"""Pinned public model ids derived from the configured completion backends.

Each enabled agent resolves ``<agent>/<alias>`` for every configured CLI model alias, plus
the bare ``<agent>`` id for its first alias. Public listings contain only named models;
automatic/default shortcuts remain available for routing. Limits come from discovery
(``relay.gateway.discovery``): an alias whose limits were not observed is still listed, with
its limits reported as null until a probe succeeds.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

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

from .discovery import (
    PROBE_OUTPUT_BYTES,
    PROBE_PROMPT,
    PROBE_TIMEOUT_S,
    ModelCache,
    discover,
    report_probe_failure,
)
from .protocol import ChatRequest, GatewayError
from .runtime import GatewayRuntime

ModelState = Literal["observed", "undiscovered", "failed"]


def _model_family(model: str) -> str:
    base, _, variant = model.rpartition("-")
    return base if base and variant in {"minimal", "low", "medium", "high"} else model


@dataclass(frozen=True)
class GatewayModel:
    id: str
    agent: str
    model: str
    effort: str | None
    support: CompletionSupport
    adapter: CompletionCapable
    cost: str | None = None

    @property
    def reasoning_efforts(self) -> tuple[str, ...]:
        options = self.support.model_options.get(self.model)
        if options is not None:
            return options.reasoning_efforts
        return ("low", "medium", "high") if self.support.supports_reasoning_effort else ()

    @property
    def listed(self) -> bool:
        options = self.support.model_options.get(self.model)
        family = _model_family(self.model)
        return (
            options is None
            or options.listed
            or (family != self.model and self.id == f"{self.agent}/{family}")
        )

    def public(self, observed: ObservedModel | None) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": "relay",
            "relay": {
                "resolved_model": (
                    f"{self.agent}/{self.model if observed is None else observed.model}"
                ),
                "model_alias": self.model,
                "supports_tools": self.support.supports_tools,
                "native_max_tokens": self.support.native_max_tokens,
                "ignored_parameters": ["temperature", "top_p"],
                "context_length": None if observed is None else observed.context_length,
                "max_output_tokens": None if observed is None else observed.max_output_tokens,
                "cost_class": self.cost,
            },
        }

    def check(self, request: ChatRequest, observed: ObservedModel | None) -> None:
        if (
            request.reasoning_effort is not None
            and request.reasoning_effort not in self.reasoning_efforts
        ):
            raise GatewayError(
                400, "model does not support this reasoning effort", "reasoning_effort"
            )
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
        self.agent_aliases: dict[str, tuple[str, ...]] = {}
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
            if models is not None:
                aliases = self.aliases
            else:
                adapter_aliases: object = getattr(adapter, "default_aliases", None)
                if isinstance(adapter_aliases, (tuple, list)):
                    aliases = model_aliases(cast("Sequence[str]", adapter_aliases))
                elif name == "agy" or getattr(adapter, "id", None) == "agy":
                    aliases = (
                        model_aliases(("default", *agent.models.values()))
                        if agent.models
                        else ("default",)
                    )
                else:
                    aliases = self.aliases
            # Model enumeration may establish per-model capabilities on the adapter.
            support = adapter.inspect()
            self._supports[name] = (adapter, support)
            alias = GatewayModel(name, name, aliases[0], None, support, adapter, agent.cost)
            self._add(alias)
            self.defaults.append(alias)
            for model in aliases:
                self._add(
                    GatewayModel(f"{name}/{model}", name, model, None, support, adapter, agent.cost)
                )
            for model in aliases:
                family_id = f"{name}/{_model_family(model)}"
                if family_id not in self.models:
                    # Keep the selected native ID behind the family alias. A stripped name
                    # need not be accepted by the CLI, especially with a lone variant.
                    self._add(replace(self.models[f"{name}/{model}"], id=family_id))
            self.agent_aliases[name] = tuple(
                dict.fromkeys(
                    model.model
                    for model in self.models.values()
                    if model.agent == name
                    and (
                        model.id == name
                        or (model.listed and model.id == f"{name}/{_model_family(model.model)}")
                    )
                )
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
                aliases = self.agent_aliases.get(agent, self.aliases)
                skip = frozenset(alias for alias in aliases if (agent, alias) in self._attempted)
                result = discover(
                    adapter,
                    support.profile,
                    aliases,
                    self.cache,
                    probe_missing=probe,
                    refresh=refresh,
                    skip=frozenset() if refresh else skip,
                    cancel=self._cancel,
                    timeout_s=timeout_s,
                    probe_model=probe_model,
                )
                self._attempted.update((agent, alias) for alias in result.probed)
                for alias in aliases:
                    if alias in result.observed:
                        observed[(agent, alias)] = result.observed[alias]
                        failed.discard((agent, alias))
                    elif alias in result.failed:
                        observed.pop((agent, alias), None)
                        failed.add((agent, alias))
        finally:
            self._observed, self._failed = observed, frozenset(failed)

    async def discover(self, runtime: GatewayRuntime) -> None:
        """Discover once per catalog; concurrent callers share the initial probe run."""
        if self._task is not None and self._task.done():
            return
        if self._task is None:
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
                    if exc.code == "request_cancelled":
                        raise
                    report_probe_failure(adapter, alias, exc)
                    return None
                except OSError as exc:
                    report_probe_failure(adapter, alias, exc)
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
            if not self.defaults:
                return None
            known = [observed.get((model.agent, model.model)) for model in self.defaults]
            limits = [entry for entry in known if entry is not None]
            complete = len(limits) == len(known)
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
                    "context_length": (
                        min(entry.context_length for entry in limits) if complete else None
                    ),
                    "max_output_tokens": (
                        min(entry.max_output_tokens for entry in limits) if complete else None
                    ),
                },
            }
        model = self.models.get(model_id)
        if model is None:
            return None
        return model.public(observed.get((model.agent, model.model)))

    def public(self, model_id: str) -> dict[str, Any]:
        value = self._public(model_id, self._observed)
        if value is None:
            raise GatewayError(404, "unknown model", "model", "model_not_found")
        return value

    def listing(self) -> dict[str, Any]:
        """Named families only; limits are null until a probe observes them."""
        observed = self._observed
        ids = [
            model.id
            for model in self.models.values()
            if model.listed
            and model.id == f"{model.agent}/{_model_family(model.model)}"
            and _model_family(model.model) not in {"auto", "default"}
        ]
        entries = [self._public(model_id, observed) for model_id in ids]
        for entry in entries:
            if entry is not None:
                entry["relay"]["model_alias"] = entry["id"].split("/", 1)[1]
                entry["relay"]["resolved_model"] = _model_family(entry["relay"]["resolved_model"])
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
                503, "no enabled completion backend available", "model", "model_unavailable"
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
