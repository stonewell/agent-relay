"""`ContextManifest` (design section 5.6, phase 2c).

A manifest records exactly what a recipient was given: the hash of the delivered payload, where
every piece came from (identity, content hash, line range), why it was selected, what was
omitted and the byte and estimated-token counts. The retained payload is the inspector's source
of truth; the manifest hash feeds the attempt's input key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import SpecModel, VersionedSpec

EntryKind = Literal["repo", "artifact", "result", "operator", "summary"]


class ManifestLimits(SpecModel):
    max_bytes: int = Field(ge=1)
    max_estimated_tokens: int = Field(ge=1)


class ManifestEntry(SpecModel):
    entry_id: str
    kind: EntryKind
    source: str
    """Stable identity: `<commit>:<path>`, `<run>/<node>@<attempt>:<artifact>`, a node field, ..."""
    content_sha256: str | None = None
    """Hash of the selected content as delivered (None when the entry was omitted unread)."""
    lines: tuple[int, int] | None = None
    source_bytes: int | None = None
    bytes: int = 0
    estimated_tokens: int = 0
    required: bool = True
    included: bool = True
    omitted_reason: str | None = None
    selection: str
    """Why it was selected: `dependency`, `coordinator-request`, `operator`, `instruction`, ..."""
    note: str | None = None
    """For example `symlink: link data only, not dereferenced`."""
    derived_from: list[str] = []
    summarizer: str | None = None


class ContextManifest(VersionedSpec):
    manifest_id: str
    recipient: str
    created_at: str
    limits: ManifestLimits
    overhead_bytes: int = Field(default=0, ge=0)
    """Instructions and schema counted against the limits but not part of the payload."""
    entries: list[ManifestEntry] = []
    payload_sha256: str
    payload_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
