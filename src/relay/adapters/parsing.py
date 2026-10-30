"""JSON Schema conversion for structured completion output."""

from __future__ import annotations

from typing import Any, cast


def _accepts_null(node: Any) -> bool:
    """True when a JSON Schema node already accepts ``null``."""
    if not isinstance(node, dict):
        return False
    schema = cast("dict[str, Any]", node)
    node_type = schema.get("type")
    if node_type == "null":
        return True
    if isinstance(node_type, list) and "null" in node_type:
        return True
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and any(
            _accepts_null(branch) for branch in cast("list[Any]", branches)
        ):
            return True
    return False


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a generated JSON Schema to the strict form Codex requires (section 5.4).

    Every object lists all of its properties in ``required``, sets
    ``additionalProperties: false``, and makes each optional property nullable. The
    ``$schema`` key is dropped so the inline value matches what the CLI expects.
    """

    def strictify(node: Any) -> Any:
        if isinstance(node, list):
            return [strictify(item) for item in cast("list[Any]", node)]
        if not isinstance(node, dict):
            return node
        original = cast("dict[str, Any]", node)
        result: dict[str, Any] = {key: strictify(value) for key, value in original.items()}
        props = result.get("properties")
        if isinstance(props, dict):
            properties = cast("dict[str, Any]", props)
            required = set(cast("list[str]", original.get("required", ())))
            converted: dict[str, Any] = {}
            for name, prop_schema in properties.items():
                if name not in required and not _accepts_null(prop_schema):
                    prop_schema = {"anyOf": [prop_schema, {"type": "null"}]}
                converted[name] = prop_schema
            result["properties"] = converted
            result["required"] = list(converted)
            result["additionalProperties"] = False
        return result

    converted = strictify(schema)
    converted.pop("$schema", None)
    return converted
