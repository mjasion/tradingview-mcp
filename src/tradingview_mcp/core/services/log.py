"""Human-readable logging for the MCP server.

Goal: answer "what is this thing actually doing right now?" without forcing
the operator to read source code. Lines look like::

    14:02:18 │ portfolio_scan: 3 symbols (AAPL, MSFT, NVDA)
    14:02:18 │   ↪ AAPL: fetching TA, earnings, dividends, news…
    14:02:18 │   ↪ MSFT: cache hit on earnings (5h old)
    14:02:19 │   ↪ AAPL: 4 news items (1 dropped as stale)
    14:02:19 │ portfolio_scan done in 1.2s — 2 flagged, 1 errored

Logs go to **stderr** so the stdio MCP transport (which uses stdout for
JSON-RPC frames) is not corrupted.

Configure via env var ``TRADINGVIEW_MCP_LOG_LEVEL`` (default ``INFO``).
``DEBUG`` adds per-HTTP-call detail; ``WARNING`` quiets routine activity.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional


_INITIALIZED = False
_LOGGER_NAME = "tvmcp"


class _Formatter(logging.Formatter):
    """Compact, human-friendly format. No module:line — just time + message."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        return f"{ts} │ {record.getMessage()}"


def setup(level: Optional[str] = None) -> None:
    """Initialise the root ``tvmcp`` logger. Idempotent."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    level_name = (level or os.environ.get("TRADINGVIEW_MCP_LOG_LEVEL") or "INFO").upper()
    level_value = getattr(logging, level_name, logging.INFO)

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level_value)
    logger.propagate = False  # don't double-print via root handler

    # Clear any pre-existing handlers (defensive on reload).
    for h in list(logger.handlers):
        logger.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_Formatter())
    logger.addHandler(handler)

    _INITIALIZED = True


def get_logger(suffix: str | None = None) -> logging.Logger:
    """Return ``tvmcp`` (or ``tvmcp.<suffix>``) — auto-initialises on first use."""
    if not _INITIALIZED:
        setup()
    name = _LOGGER_NAME if not suffix else f"{_LOGGER_NAME}.{suffix}"
    return logging.getLogger(name)


def log_tool_call(tool: str, **fields: object) -> None:
    """Single-line entry log for an MCP tool invocation. Keeps args terse."""
    log = get_logger()
    if not fields:
        log.info("→ %s", tool)
        return
    pretty = ", ".join(f"{k}={_pretty(v)}" for k, v in fields.items() if v is not None)
    log.info("→ %s(%s)", tool, pretty)


def _pretty(v: object) -> str:
    if isinstance(v, list):
        if len(v) <= 5:
            return "[" + ", ".join(str(x) for x in v) + "]"
        return f"[{v[0]}, {v[1]}, … {len(v)} items]"
    if isinstance(v, str) and len(v) > 40:
        return f"{v[:37]!r}…"
    return repr(v) if isinstance(v, str) else str(v)
