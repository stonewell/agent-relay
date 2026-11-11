"""Per-request log context: request id and agent on every gateway, runtime and adapter line.

Values propagate to completion worker threads through ``contextvars.copy_context()``
(``GatewayRuntime._call``); a plain ``threading.Thread`` does not inherit them.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

REQUEST_ID: ContextVar[str | None] = ContextVar("relay_request_id", default=None)
AGENT: ContextVar[str | None] = ContextVar("relay_agent", default=None)


class ContextFilter(logging.Filter):
    """Add ``request_id`` and ``agent`` attributes (``-`` when unset); never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get() or "-"
        record.agent = AGENT.get() or "-"
        return True
