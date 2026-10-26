"""Spec models, loading and JSON Schema export (design section 5, section 15)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ..yamlio import load_yaml
from .answers import InputAnswers, Submission
from .common import SPEC_VERSION
from .coordination import CoordinatorResponse
from .graph import Graph, GraphNode, Policy
from .result import OpenQuestion, ResultSpec
from .route import RouteEntry
from .task import TaskSpec

__all__ = [
    "SCHEMAS",
    "SPEC_VERSION",
    "CoordinatorResponse",
    "Graph",
    "GraphNode",
    "InputAnswers",
    "OpenQuestion",
    "Policy",
    "ResultSpec",
    "RouteEntry",
    "Submission",
    "TaskSpec",
    "export_schemas",
    "load_spec",
    "parse_spec",
    "schema_for",
]

M = TypeVar("M", bound=BaseModel)

SCHEMAS: dict[str, type[BaseModel]] = {
    "task-spec": TaskSpec,
    "result-spec": ResultSpec,
    "graph": Graph,
    "input-answers": InputAnswers,
    "route-entry": RouteEntry,
    "coordinator-response": CoordinatorResponse,
}


def parse_spec(model: type[M], text: str) -> M:
    """Parse YAML or JSON text (JSON is valid YAML) and validate it against `model`."""
    data: Any = load_yaml(text)
    return model.model_validate(data)


def load_spec(model: type[M], path: Path) -> M:
    return parse_spec(model, path.read_text(encoding="utf-8"))


def schema_for(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def export_schemas(directory: Path) -> list[Path]:
    """Write `schemas/*.json`, the single source for validation and the CLIs' schema flags."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in SCHEMAS.items():
        target = directory / f"{name}.json"
        text = json.dumps(schema_for(model), indent=2, sort_keys=True) + "\n"
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
    return written
