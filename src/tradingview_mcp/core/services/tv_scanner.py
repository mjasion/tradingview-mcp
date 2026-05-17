"""TradingView scanner wrapper — single chokepoint with retry, outage
detection, and short-TTL caching.

Why this exists: every TA tool in the MCP (coin_analysis,
multi_agent_analysis, multi_timeframe_analysis, commodity_snapshot, volume
scanners, EGX tools) calls ``tradingview_ta.get_multiple_analysis``, which
under the hood hits ``scanner.tradingview.com/<screener>/scan``. That host
suffers periodic weekend / maintenance blips where it returns empty bodies
or HTML — the library then explodes with ``json.JSONDecodeError: Expecting
value: line 1 column 1 (char 0)``. Without a shared wrapper, every tool
re-derives the same flaky semantics, and callers see a confusing
``Analysis failed: …`` that is indistinguishable from "ticker doesn't exist".

This module fixes three things:

1.  **Retry**: 3 attempts with exponential backoff (0.4s → 1s → 2.5s) on
    network or JSON-decode errors. Single-flight latency under the worst
    case is ~4s — within MCP tolerance, well above transient blip recovery.
2.  **Outage classification**: a JSONDecodeError or empty result after
    retries raises :class:`TVScannerUnavailable`, distinct from
    :class:`TVScannerEmpty` ("symbol not found"). Callers can map these to
    user-facing ``upstream_status: "down"`` vs ``error: "no data"``.
3.  **Short cache**: each ``(screener, interval, sorted-symbols)`` tuple is
    cached for ``_CACHE_TTL`` seconds (90s default). When the scanner
    flickers, retries within the TTL window are free, and a flapping host
    won't dominate ``portfolio_scan`` latency.

The wrapper is intentionally a thin function, not a class — every existing
caller can swap ``get_multiple_analysis(...)`` for ``ta_call(...)`` with no
other changes.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterable, Optional

from tradingview_mcp.core.services.log import get_logger

_log = get_logger("tv_scanner")

try:
    from tradingview_ta import get_multiple_analysis  # type: ignore
    _TA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TA_AVAILABLE = False

    def get_multiple_analysis(*_a, **_kw):  # type: ignore
        raise RuntimeError("tradingview_ta is not installed")


# ── Public exceptions ─────────────────────────────────────────────────────────

class TVScannerUnavailable(RuntimeError):
    """scanner.tradingview.com is degraded — empty body / non-JSON / network."""


class TVScannerEmpty(RuntimeError):
    """Scanner replied successfully but no rows matched (likely bad symbol)."""


# ── Tuning ────────────────────────────────────────────────────────────────────

_RETRY_DELAYS = (0.4, 1.0, 2.5)  # seconds; len = attempts - 1
_CACHE_TTL = 90  # seconds

# In-memory cache. We can't use the on-disk @cached decorator because
# tradingview_ta returns ``Analysis`` objects (not JSON-serializable). A
# process-local dict is fine — the cache only needs to absorb short bursts of
# duplicate calls (portfolio_scan running the same ticker batch back-to-back),
# not survive restarts.
_CACHE_LOCK = threading.RLock()
_CACHE: dict[tuple[str, str, tuple[str, ...]], tuple[float, dict[str, Any]]] = {}


def _is_outage(exc: BaseException) -> bool:
    """Heuristic: would another retry plausibly succeed against the same host?

    JSONDecodeError ≈ "got HTML or empty body" → yes (Cloudflare/maintenance).
    Network errors → yes. ValueErrors from tradingview_ta sometimes wrap
    the parse failure too; match on message as a safety net.
    """
    if isinstance(exc, (json.JSONDecodeError, ConnectionError, TimeoutError)):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "expecting value", "json", "connection", "timed out", "timeout",
        "remote end closed", "max retries", "temporarily unavailable",
    ))


# ── Core call (uncached) ──────────────────────────────────────────────────────

def _do_call(screener: str, interval: str, symbols: list[str]) -> dict[str, Any]:
    """Single TA call with retry/backoff. Raises TVScannerUnavailable on outage."""
    last_exc: Optional[BaseException] = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            result = get_multiple_analysis(
                screener=screener, interval=interval, symbols=symbols
            )
            if result is None:
                # tradingview_ta sometimes returns None instead of raising.
                # Treat as transient and retry.
                raise TVScannerUnavailable("scanner returned None")
            return result
        except (TVScannerUnavailable, json.JSONDecodeError,
                ConnectionError, TimeoutError) as exc:
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 — classify by message
            if _is_outage(exc):
                last_exc = exc
            else:
                # Unrelated error (bad screener name, type error, etc.) —
                # don't retry, propagate so the caller can surface it.
                raise

        if attempt < len(_RETRY_DELAYS):
            delay = _RETRY_DELAYS[attempt]
            _log.warning(
                "tv_scanner retry %d/%d after %.1fs (%s: %s)",
                attempt + 1, len(_RETRY_DELAYS), delay,
                type(last_exc).__name__, last_exc,
            )
            time.sleep(delay)

    # Exhausted retries.
    raise TVScannerUnavailable(
        f"scanner.tradingview.com unavailable after "
        f"{len(_RETRY_DELAYS) + 1} attempts: {last_exc}"
    ) from last_exc


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_get(key: tuple[str, str, tuple[str, ...]]) -> Optional[dict[str, Any]]:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        expires_at, value = entry
        if expires_at < time.time():
            _CACHE.pop(key, None)
            return None
        return value


def _cache_put(key: tuple[str, str, tuple[str, ...]], value: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + _CACHE_TTL, value)


def reset_cache_for_tests() -> None:
    """Drop the in-memory cache. Tests use this between scenarios."""
    with _CACHE_LOCK:
        _CACHE.clear()


def ta_call(
    screener: str,
    interval: str,
    symbols: Iterable[str],
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Drop-in replacement for ``tradingview_ta.get_multiple_analysis``.

    Args:
        screener:  TradingView screener name (``"america"``, ``"crypto"``, …).
        interval:  TradingView interval string (``"15m"``, ``"4h"``, …).
        symbols:   Iterable of ``EXCHANGE:TICKER`` strings.
        use_cache: When False, bypass the 90s cache (e.g. for retries the
                   caller already coordinated, or to force fresh data).

    Returns:
        Dict keyed by symbol → ``Analysis`` object (or ``None`` for misses).
        The values match ``tradingview_ta`` exactly so existing callers
        don't need to change how they read ``.indicators``.

    Raises:
        TVScannerUnavailable: After retries, the upstream is still degraded.
        TVScannerEmpty:       Upstream returned ok but no symbols matched.
        ImportError:          ``tradingview_ta`` not installed.
    """
    if not _TA_AVAILABLE:
        raise ImportError("tradingview_ta is not installed; run `uv sync`")

    syms = sorted(set(s for s in symbols if s))
    if not syms:
        raise TVScannerEmpty("no symbols supplied")

    key = (screener, interval, tuple(syms))
    if use_cache:
        cached_value = _cache_get(key)
        if cached_value is not None:
            _log.debug("tv_scanner cache hit for %s/%s (%d symbols)", screener, interval, len(syms))
            return cached_value

    result = _do_call(screener, interval, syms)

    if not result:
        raise TVScannerEmpty(f"scanner returned no rows for {syms}")

    if use_cache:
        _cache_put(key, result)
    return result


# ── Helpers for callers that prefer the dict-or-error shape ──────────────────

def ta_call_or_error(
    screener: str,
    interval: str,
    symbols: Iterable[str],
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Like ``ta_call`` but never raises. Returns either:

    * ``{"_ok": True,  "analysis": {sym: Analysis, ...}}`` on success
    * ``{"_ok": False, "upstream_status": "down", "error": "...", "detail": ...}``
      when the scanner is unavailable
    * ``{"_ok": False, "upstream_status": "empty", "error": "no_data", ...}``
      when symbols returned nothing

    Useful inside batch loops where one bad ticker should not abort the rest.
    """
    try:
        return {"_ok": True, "analysis": ta_call(screener, interval, symbols, use_cache=use_cache)}
    except TVScannerUnavailable as e:
        return {
            "_ok": False,
            "upstream_status": "down",
            "error": "tradingview_scanner_unavailable",
            "detail": str(e),
            "retry_hint": "scanner.tradingview.com is degraded — retry in 60-120s, "
                          "or use Yahoo fallback (analyze_coin auto-falls back).",
        }
    except TVScannerEmpty as e:
        return {
            "_ok": False,
            "upstream_status": "empty",
            "error": "no_data",
            "detail": str(e),
        }
