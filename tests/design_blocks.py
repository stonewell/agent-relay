"""Extract fenced code blocks from docs/design.md, tagged with their section heading."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DESIGN = Path(__file__).resolve().parents[1] / "docs" / "design.md"

_FENCE = re.compile(r"^```(\w*)\s*$")


@dataclass(frozen=True)
class Block:
    lang: str
    heading: str
    text: str
    line: int


def design_blocks() -> list[Block]:
    blocks: list[Block] = []
    heading = ""
    lang: str | None = None
    buf: list[str] = []
    start = 0
    for number, line in enumerate(DESIGN.read_text(encoding="utf-8").splitlines(), start=1):
        if lang is None:
            if line.startswith("#"):
                heading = line.lstrip("# ").strip()
            elif (m := _FENCE.match(line)) is not None:
                lang, buf, start = m.group(1), [], number
        elif line.strip() == "```":
            blocks.append(Block(lang, heading, "\n".join(buf), start))
            lang = None
        else:
            buf.append(line)
    return blocks


def blocks_in(section_prefix: str, lang: str) -> list[Block]:
    return [b for b in design_blocks() if b.heading.startswith(section_prefix) and b.lang == lang]
