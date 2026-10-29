"""Bounded JSON Schema subset and independent completed-envelope validation.

Supported keywords are listed in SCHEMA_KEYS. References are local JSON pointers;
cycles, regexes, unique-item checks, remote resources and unrecognized dialect extensions
are rejected. ANNOTATION_KEYS are accepted but never asserted (validation uses Draft 2020-12
without a format checker), so they cannot weaken argument validation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Protocol, cast

from .protocol import ChatRequest, GatewayError, NamedChoice, check_json_values
from .render import canonical

# Common generator output (zod-to-json-schema, pydantic, OpenAPI). None of these constrain
# instances here; `nullable` is OpenAPI-only and ignored, so it can only make validation
# stricter (null still needs an explicit "null" type), never looser.
ANNOTATION_KEYS = frozenset(
    {
        "$schema",
        "$comment",
        "description",
        "title",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "format",
        "contentEncoding",
        "contentMediaType",
        "nullable",
    }
)

SCHEMA_KEYS = ANNOTATION_KEYS | frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "oneOf",
        "allOf",
        "$defs",
        "definitions",
        "$ref",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
    }
)

MAX_SCHEMA_WEIGHT = 10000
MAX_VALIDATION_WORK = 200000


def _value_weight(value: Any) -> int:
    """Conservative structural/string work, including enum/const comparison operands."""
    pending: list[Any] = [value]
    weight = 0
    while pending:
        current = pending.pop()
        weight += 1
        if isinstance(current, str):
            weight += (len(current) + 255) // 256
        elif isinstance(current, dict):
            for key, child in cast("dict[str, Any]", current).items():
                pending.extend((key, child))
        elif isinstance(current, list):
            pending.extend(cast("list[Any]", current))
    return weight


class _Validator(Protocol):
    def iter_errors(self, instance: Any) -> Iterator[Any]: ...


def validate_schema(schema: dict[str, Any]) -> int:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        check_json_values(schema)
    except ValueError as exc:
        raise GatewayError(400, str(exc), "tools") from exc
    if len(canonical(schema).encode("utf-8")) > 65536:
        raise GatewayError(400, "tool schema exceeds 64 KiB", "tools")
    visits = 0
    weight = 0

    def walk(node: Any, depth: int, refs: frozenset[str]) -> None:
        nonlocal visits, weight
        visits += 1
        weight += 1
        if visits > 2048 or depth > 24:
            raise GatewayError(400, "tool schema exceeds depth/complexity limit", "tools")
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise GatewayError(400, "schema must be an object or boolean", "tools")
        node = cast("dict[str, Any]", node)
        for keyword in ("enum", "const", "required"):
            if keyword in node:
                weight += _value_weight(node[keyword])
        weight += len(node)
        if weight > MAX_SCHEMA_WEIGHT:
            raise GatewayError(400, "tool schema exceeds expanded validation weight", "tools")
        for key in node:
            if key not in SCHEMA_KEYS:
                raise GatewayError(400, f"unsupported schema keyword: {key}", "tools")
        ref = node.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/") or ref in refs:
                raise GatewayError(
                    400, "only non-recursive local schema references supported", "tools"
                )
            target: Any = schema
            try:
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError, IndexError) as exc:
                raise GatewayError(400, "unresolved local schema reference", "tools") from exc
            walk(target, depth + 1, refs | {ref})
        for key in ("properties", "$defs", "definitions"):
            children = node.get(key, {})
            if not isinstance(children, dict):
                raise GatewayError(400, f"{key} must be an object", "tools")
            for child in cast("dict[str, Any]", children).values():
                walk(child, depth + 1, refs)
        for key in ("additionalProperties", "items"):
            if key in node:
                walk(node[key], depth + 1, refs)
        for key in ("anyOf", "oneOf", "allOf"):
            if key in node:
                if not isinstance(node[key], list):
                    raise GatewayError(400, f"{key} must be an array", "tools")
                for child in cast("list[Any]", node[key]):
                    walk(child, depth + 1, refs)

    walk(schema, 0, frozenset())
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise GatewayError(400, "invalid tool argument schema", "tools") from exc
    if schema.get("type") != "object":
        raise GatewayError(400, "tool argument schema must declare type object", "tools")
    return weight


def prepare_envelope(request: ChatRequest) -> dict[str, Any] | None:
    if not request.tools:
        return None
    total_weight = sum(validate_schema(tool.function.parameters) for tool in request.tools)
    if total_weight > MAX_SCHEMA_WEIGHT:
        raise GatewayError(400, "tools exceed total validation weight", "tools")
    # Keep each function's schema in its own root for independent local $ref resolution.
    # The native schema requests the transport shape; original argument constraints are
    # checked below and are never weakened or converted to provider-required fields.
    calls: dict[str, Any] = {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "arguments"],
            "properties": {
                "name": {"type": "string", "enum": [t.function.name for t in request.tools]},
                "arguments": {"type": "object"},
            },
        },
    }
    if request.tool_choice == "none":
        calls["maxItems"] = 0
    if request.tool_choice == "required":
        calls["minItems"] = 1
    if request.tool_choice != "none" and (
        not request.parallel_tool_calls or isinstance(request.tool_choice, NamedChoice)
    ):
        calls["maxItems"] = 1
    if isinstance(request.tool_choice, NamedChoice):
        calls["minItems"] = 1
        calls["items"]["properties"]["name"] = {"const": request.tool_choice.function.name}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["content", "tool_calls"],
        "properties": {"content": {"type": ["string", "null"]}, "tool_calls": calls},
    }


class EnvelopeError(ValueError):
    """An invalid completed envelope; the only repairable failure."""


def validate_envelope(
    raw: str, request: ChatRequest, schema: dict[str, Any]
) -> tuple[str | None, list[dict[str, Any]]]:
    from jsonschema import Draft202012Validator

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        value = json.loads(raw, parse_constant=reject_constant)
        check_json_values(value)
        # Bound the schema × instance cross-product before entering the validator. Merely
        # limiting schemas and instances independently still allows minutes of nested work.
        work = validate_schema(schema) * _value_weight(value)
        if work > MAX_VALIDATION_WORK:
            raise EnvelopeError("tool envelope exceeds validation work budget")
        error = next(cast(_Validator, Draft202012Validator(schema)).iter_errors(value), None)
        if error:
            raise EnvelopeError(error.message[:2048])
        envelope = cast("dict[str, Any]", value)
        definitions = {tool.function.name: tool.function.parameters for tool in request.tools or []}
        calls = cast("list[dict[str, Any]]", envelope["tool_calls"])
        costs = {name: validate_schema(definition) for name, definition in definitions.items()}
        for call in calls:
            work += costs[call["name"]] * _value_weight(call["arguments"])
            if work > MAX_VALIDATION_WORK:
                raise EnvelopeError("tool arguments exceed validation work budget")
        for call in calls:
            error = next(
                cast(_Validator, Draft202012Validator(definitions[call["name"]])).iter_errors(
                    call["arguments"]
                ),
                None,
            )
            if error:
                raise EnvelopeError(f"invalid arguments for {call['name']}: {error.message[:1024]}")
        if envelope["content"] is None and not calls:
            # An empty assistant turn is never a valid completion; let it be repaired.
            raise EnvelopeError("envelope must contain content or at least one tool call")
        return envelope["content"], calls
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, EnvelopeError):
            raise
        raise EnvelopeError("response must be a complete valid JSON tool envelope") from exc
