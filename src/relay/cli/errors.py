"""Command-boundary errors (design section 14)."""

from __future__ import annotations

import json
import sys


def report_error(message: str, code: int, as_json: bool) -> int:
    print(
        json.dumps({"error": message}, ensure_ascii=False) if as_json else message, file=sys.stderr
    )
    return code
