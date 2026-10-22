"""Strict YAML reading shared by the spec and configuration loaders."""

from __future__ import annotations

from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class _StrictLoader(yaml.SafeLoader):
    """Safe loader that keeps 2026-10-22 style values as strings and rejects duplicate keys."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if key in seen:
                raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_StrictLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_yaml(text: str) -> Any:
    """Parse YAML or JSON text; raises `yaml.YAMLError` on syntax errors or duplicate keys."""
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - safe loader subclass
