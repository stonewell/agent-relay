"""`input_answers.yaml` (design section 5.5)."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .common import NodeId, SpecModel, VersionedSpec

Provenance = Literal["operator-tty", "operator-cli", "host-submitted", "host-attested"]
AnswerValue = str | list[str]


class Submission(SpecModel):
    submission_id: str = Field(min_length=1)
    question_revision: int = Field(ge=1)
    provenance: Provenance
    submitted_at: str
    answers: dict[str, AnswerValue]

    def content_hash(self) -> str:
        """Hash of the answers only; this, not the content, appears in events (section 11)."""
        canonical = json.dumps(self.answers, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class InputAnswers(VersionedSpec):
    node: NodeId
    submissions: list[Submission] = []

    @model_validator(mode="after")
    def _unique_submissions(self) -> InputAnswers:
        ids = [s.submission_id for s in self.submissions]
        if len(ids) != len(set(ids)):
            raise ValueError("submission_id values must be unique")
        return self
