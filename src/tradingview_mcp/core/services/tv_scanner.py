"""TradingView scanner wrapper — single chokepoint with retry, outage and
rate-limit detection, a circuit breaker, and caching.

Why this exists: every TA tool in the MCP (coin_analysis,
multi_agent_analysis, multi_timeframe_analysis, commodity_snapshot, volume
scanners, EGX tools) needs ``tradingview_ta.get_multiple_analysis``, which
under the hood hits ``scanner.tradingview.com/<screener>/scan``. Two very
different failures used to look identical:

* **Blips** — weekend / maintenance windows where the host answers 200 with an
  empty body or HTML.
* **Rate limiting** — CloudFront answers ``429`` with an *empty body* once an
  egress IP has asked too often. Verified 2026-10-17: every request from the
  home IP got ``429 len=0``, host and container alike, while the same payload
  had worked minutes earlier.

``tradingview_ta`` never looks at the status code — it calls
``json.loads(response.text)`` — so both surface as ``JSONDecodeError:
Expecting value: line 1 column 1``. Retrying a 429 four times in four seconds
only digs the hole deeper. So this module makes the request itself (same
payload, same parser as the library) and handles each case on its own terms:

1.  **Blip retry**: 3 retries with backoff (0.4s → 1s → 2.5s) on network or
    JSON-decode errors, then :class:`TVScannerUnavailable`.
2.  **429 backoff**: a 429 closes a *process-global* gate for 5s, doubling per
    consecutive 429 (cap 60s, ``Retry-After`` wins when sent). Every thread
    waits on the same gate, so four portfolio workers don't each re-trip the
    limit. A call keeps waiting and retrying until it succeeds or its own
    budget (60s) runs out — that's the "answer slower, but answer" path.
3.  **Circuit breaker**: a call that exhausts its budget holds the gate shut
    for the breaker TTL (5 min). While it's shut, calls that couldn't make it
    in their budget fail fast with :class:`TVScannerRateLimited`
    (``reason="circuit_open"``) instead of each burning another minute. The
    first call after the TTL probes; any success closes the gate.
4.  **Cache**: each ``(screener, interval, sorted-symbols)`` result is fresh
    for 90s. Entries are kept up to 6h so callers that pass
    ``allow_stale=True`` (and promise to report it) can get the last good
    reading during an outage — capped at 4 bars of the interval, so a 15m
    request never gets anything older than an hour.

Tunables (env):
    TRADINGVIEW_MCP_TV_429_BACKOFF      first 429 wait, seconds (default 5)
    TRADINGVIEW_MCP_TV_429_MAX_BACKOFF  cap on one wait (default 60)
    TRADINGVIEW_MCP_TV_429_BUDGET       per-call total wait before giving up (default 60)
    TRADINGVIEW_MCP_TV_BREAKER_TTL      gate hold after a call gives up (default 300; 0 disables)
    TRADINGVIEW_MCP_TV_STALE_TTL        how long results stay usable as stale (default 21600; 0 disables)
"""
from __future__ import annotations

import json
import os
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional

from tradingview_mcp.core.services.log import get_logger
from tradingview_mcp.core.services.rate_limiter import acquire as _rate_acquire

_log = get_logger("tv_scanner")

try:
    import requests  # type: ignore
    import tradingview_ta  # type: ignore
    from tradingview_ta.main import TradingView, calculate  # type: ignore
    _TA_AVAILABLE = True
    _REQUEST_ERRORS: tuple[type[BaseException], ...] = (requests.RequestException,)
except ImportError:  # pragma: no cover
    _TA_AVAILABLE = False
    _REQUEST_ERRORS = ()


# ── Public exceptions ─────────────────────────────────────────────────────────

class TVScannerUnavailable(RuntimeError):
    """scanner.tradingview.com is degraded — empty body / non-JSON / network."""


class TVScannerRateLimited(TVScannerUnavailable):
    """scanner.tradingview.com answered 429, or the breaker is holding off.

    A subclass of :class:`TVScannerUnavailable` so every existing outage
    handler (and the Yahoo fallback behind it) keeps working; callers that
    care tell the two apart with :func:`upstream_fields`.

    ``reason`` is ``"http_429"`` (this call saw 429s until its budget ran out)
    or ``"circuit_open"`` (another call already did; this one didn't ask).
    ``retry_after`` is seconds until the gate opens, when known.
    """

    def __init__(self, message: str, *, reason: str = "http_429",
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_after = retry_after


class TVScannerEmpty(RuntimeError):
    """Scanner replied successfully but no rows matched (likely bad symbol)."""


# ── Tuning ────────────────────────────────────────────────────────────────────

def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


_RETRY_DELAYS = (0.4, 1.0, 2.5)  # seconds; blip retries (not 429)
_CACHE_TTL = 90  # seconds
_HTTP_TIMEOUT = 20  # seconds; the library default was None (hang forever)
_429_BACKOFF = _float_env("TRADINGVIEW_MCP_TV_429_BACKOFF", 5.0)
_429_MAX_BACKOFF = _float_env("TRADINGVIEW_MCP_TV_429_MAX_BACKOFF", 60.0)
_429_BUDGET = _float_env("TRADINGVIEW_MCP_TV_429_BUDGET", 60.0)
_BREAKER_TTL = _float_env("TRADINGVIEW_MCP_TV_BREAKER_TTL", 300.0)
_STALE_TTL = _float_env("TRADINGVIEW_MCP_TV_STALE_TTL", 6 * 3600.0)

# Bar length per interval, for the stale cap. Unknown intervals get the daily
# bar (tradingview_ta itself defaults those to 1d).
_BAR_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
    "4h": 14400, "1d": 86400, "1D": 86400, "1W": 7 * 86400, "1M": 30 * 86400,
}
_STALE_BARS = 4

# Patchable clock — tests swap in a fake one so the gate's waits are instant.
_now = time.monotonic

# In-memory cache. We can't use the on-disk @cached decorator because
# tradingview_ta returns ``Analysis`` objects (not JSON-serializable). A
# process-local dict absorbs bursts of duplicate calls and doubles as the
# stale store; it doesn't need to survive restarts.
_CACHE_LOCK = threading.RLock()
_CACHE: dict[tuple[str, str, tuple[str, ...]], tuple[float, dict[str, Any]]] = {}
_CACHE_MAX = 2048


# ── Status-aware fetch ────────────────────────────────────────────────────────

def _retry_after_seconds(header: Optional[str]) -> Optional[float]:
    """Parse ``Retry-After`` (delta-seconds or HTTP date). CloudFront sends none
    today, but if TradingView ever says how long, believe it over our guess."""
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(header).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def get_multiple_analysis(*, screener: str, interval: str, symbols: list[str]) -> dict[str, Any]:
    """``tradingview_ta.get_multiple_analysis`` with the status code checked.

    Same payload, same User-Agent, same ``calculate`` parser — the returned
    ``Analysis`` objects are identical. The only differences: a 429 raises
    :class:`TVScannerRateLimited`, other non-200s raise instead of being fed
    to ``json.loads``, and the request has a timeout.
    """
    if not _TA_AVAILABLE:
        raise RuntimeError("tradingview_ta is not installed")

    indicators_key = TradingView.indicators.copy()
    payload = TradingView.data(symbols, interval, indicators_key)
    resp = requests.post(
        f"{TradingView.scan_url}{screener.lower()}/scan",
        json=payload,
        headers={"User-Agent": f"tradingview_ta/{tradingview_ta.__version__}"},
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code == 429:
        raise TVScannerRateLimited(
            "scanner.tradingview.com answered HTTP 429 (rate limited)",
            retry_after=_retry_after_seconds(resp.headers.get("Retry-After")),
        )
    if resp.status_code >= 500 or resp.status_code == 403:
        raise TVScannerUnavailable(f"scanner.tradingview.com answered http_{resp.status_code}")
    if resp.status_code != 200:
        # 400 = a payload TradingView rejects (bad column, bad screener) — a
        # bug on our side, not something a retry fixes.
        raise RuntimeError(f"scanner.tradingview.com answered http_{resp.status_code}: {resp.text[:200]}")

    rows = json.loads(resp.text)["data"]  # empty body → JSONDecodeError → blip retry
    final: dict[str, Any] = {}
    for row in rows:
        indicators = dict(zip(indicators_key, row["d"]))
        exchange, _, ticker = row["s"].partition(":")
        final[row["s"]] = calculate(
            indicators=indicators, indicators_key=indicators_key, screener=screener,
            symbol=ticker, exchange=exchange, interval=interval,
        )
    for symbol in symbols:
        final.setdefault(symbol.upper(), None)
    return final


# ── 429 gate + circuit breaker ────────────────────────────────────────────────
#
# One gate for the whole process: no TV request starts before ``until``. A 429
# pushes it out; a success resets it. ``tripped`` marks that a call gave up and
# the breaker is holding the gate for _BREAKER_TTL.

_RL_LOCK = threading.Lock()
_RL: dict[str, Any] = {"until": 0.0, "streak": 0, "tripped": False}


def _rl_note_429(retry_after: Optional[float]) -> float:
    with _RL_LOCK:
        _RL["streak"] += 1
        wait = retry_after if retry_after is not None else min(
            _429_BACKOFF * (2 ** (_RL["streak"] - 1)), _429_MAX_BACKOFF)
        wait = max(wait, 1.0)  # a 0s wait would spin the retry loop past its budget
        _RL["until"] = max(_RL["until"], _now() + wait)
        streak = _RL["streak"]
    _log.warning("TradingView rate-limited this egress (429 #%d in a row) — holding requests %.0fs",
                 streak, wait)
    return wait


def _rl_note_ok() -> None:
    with _RL_LOCK:
        was_limited = _RL["streak"] > 0
        _RL.update(until=0.0, streak=0, tripped=False)
    if was_limited:
        _log.info("TradingView answering again — 429 gate closed")


def _rl_trip(wait: float) -> float:
    """This call gave up: hold the gate for the breaker TTL (if longer)."""
    with _RL_LOCK:
        hold = max(_BREAKER_TTL, wait)
        if _BREAKER_TTL > 0:
            _RL["until"] = max(_RL["until"], _now() + hold)
            first = not _RL["tripped"]
            _RL["tripped"] = True
        else:
            first = False
        remaining = max(0.0, _RL["until"] - _now())
    if first:
        _log.warning("TradingView circuit opened for %.0fs after a call exhausted its %.0fs 429 budget",
                     remaining, _429_BUDGET)
    return remaining


def _rl_wait(deadline: float, saw_429: bool) -> None:
    """Block until the gate opens, or raise if that's past ``deadline``."""
    while True:
        with _RL_LOCK:
            wait = _RL["until"] - _now()
            tripped = _RL["tripped"]
        if wait <= 0:
            return
        if _now() + wait > deadline:
            if saw_429:
                remaining = _rl_trip(wait)
                raise TVScannerRateLimited(
                    f"scanner.tradingview.com kept answering HTTP 429 for {_429_BUDGET:.0f}s; "
                    f"retry in ~{remaining:.0f}s",
                    reason="http_429", retry_after=remaining,
                )
            raise TVScannerRateLimited(
                "scanner.tradingview.com is rate-limiting this egress "
                f"({'circuit open' if tripped else 'backing off'}); not queried, "
                f"retry in ~{wait:.0f}s",
                reason="circuit_open", retry_after=wait,
            )
        time.sleep(wait)


def rate_limit_state() -> Optional[dict[str, Any]]:
    """``None`` when TradingView is being asked normally; otherwise a small
    block callers can put in a response so the consumer sees *why* it got a
    fallback and when it is worth asking again."""
    with _RL_LOCK:
        wait = _RL["until"] - _now()
        if wait <= 0 and not _RL["streak"]:
            return None
        return {
            "state": "open" if _RL["tripped"] else "backing_off",
            "consecutive_429": _RL["streak"],
            "retry_after_seconds": max(0, round(wait)),
        }


def reset_rate_limit_for_tests() -> None:
    with _RL_LOCK:
        _RL.update(until=0.0, streak=0, tripped=False)


def _is_outage(exc: BaseException) -> bool:
    """Heuristic: would another retry plausibly succeed against the same host?

    JSONDecodeError ≈ "got HTML or empty body" → yes (Cloudflare/maintenance).
    Network errors → yes. ValueErrors from tradingview_ta sometimes wrap
    the parse failure too; match on message as a safety net.
    """
    if isinstance(exc, (json.JSONDecodeError, ConnectionError, TimeoutError) + _REQUEST_ERRORS):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "expecting value", "json", "connection", "timed out", "timeout",
        "remote end closed", "max retries", "temporarily unavailable",
    ))


# ── Core call (uncached) ──────────────────────────────────────────────────────

def _do_call(screener: str, interval: str, symbols: list[str]) -> dict[str, Any]:
    """TA call with blip retries and 429 backoff.

    Reached only on a cache miss, so this is the one chokepoint for every
    ``tradingview_ta`` consumer (scanner, screener, multi-agent, commodity,
    egx). Each attempt takes a global rate-limit slot, and re-checks the 429
    gate after queueing for it — another thread may have hit a 429 meanwhile.
    """
    deadline = _now() + _429_BUDGET
    saw_429 = False
    blips = 0
    last_exc: Optional[BaseException] = None
    while True:
        _rl_wait(deadline, saw_429)
        _rate_acquire("scanner.tradingview.com")
        with _RL_LOCK:
            gate_shut = _RL["until"] > _now()
        if gate_shut:
            continue
        try:
            result = get_multiple_analysis(
                screener=screener, interval=interval, symbols=symbols
            )
            if result is None:
                # tradingview_ta sometimes returns None instead of raising.
                # Treat as transient and retry.
                raise TVScannerUnavailable("scanner returned None")
            _rl_note_ok()
            return result
        except TVScannerRateLimited as exc:
            saw_429 = True
            _rl_note_429(exc.retry_after)
            continue
        except (TVScannerUnavailable, json.JSONDecodeError,
                ConnectionError, TimeoutError) + _REQUEST_ERRORS as exc:
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 — classify by message
            if _is_outage(exc):
                last_exc = exc
            else:
                # Unrelated error (bad screener name, type error, etc.) —
                # don't retry, propagate so the caller can surface it.
                raise

        if blips >= len(_RETRY_DELAYS):
            break
        delay = _RETRY_DELAYS[blips]
        blips += 1
        _log.warning(
            "tv_scanner retry %d/%d after %.1fs (%s: %s)",
            blips, len(_RETRY_DELAYS), delay,
            type(last_exc).__name__, last_exc,
        )
        time.sleep(delay)

    # Exhausted retries.
    raise TVScannerUnavailable(
        f"scanner.tradingview.com unavailable after "
        f"{len(_RETRY_DELAYS) + 1} attempts: {last_exc}"
    ) from last_exc


# ── Cache helpers ─────────────────────────────────────────────────────────────

class StaleAnalysis(dict):
    """A cached ``ta_call`` result served because TradingView couldn't be asked.

    Behaves exactly like the fresh dict; ``age_seconds`` and ``reason`` say how
    old it is and why. Only returned to callers that passed ``allow_stale=True``
    — they must put that in their response (see ``stale_fields``).
    """

    def __init__(self, value: dict[str, Any], *, age_seconds: float, reason: str) -> None:
        super().__init__(value)
        self.age_seconds = age_seconds
        self.reason = reason


def _stale_limit(interval: str) -> float:
    bar = _BAR_SECONDS.get(interval, _BAR_SECONDS["1d"])
    return min(_STALE_TTL, _STALE_BARS * bar)


def _cache_get(key: tuple[str, str, tuple[str, ...]], max_age: float = _CACHE_TTL
               ) -> Optional[tuple[float, dict[str, Any]]]:
    """``(age_seconds, value)`` when an entry younger than ``max_age`` exists."""
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        stored_at, value = entry
        age = time.time() - stored_at
        if age > max(_CACHE_TTL, _STALE_TTL):
            _CACHE.pop(key, None)
            return None
        if age > max_age:
            return None
        return age, value


def _cache_put(key: tuple[str, str, tuple[str, ...]], value: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            horizon = time.time() - max(_CACHE_TTL, _STALE_TTL)
            for k in [k for k, (ts, _) in _CACHE.items() if ts < horizon]:
                del _CACHE[k]
            if len(_CACHE) >= _CACHE_MAX:  # still full of live entries: drop the oldest
                del _CACHE[min(_CACHE, key=lambda k: _CACHE[k][0])]
        _CACHE[key] = (time.time(), value)


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
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Drop-in replacement for ``tradingview_ta.get_multiple_analysis``.

    Args:
        screener:    TradingView screener name (``"america"``, ``"crypto"``, …).
        interval:    TradingView interval string (``"15m"``, ``"4h"``, …).
        symbols:     Iterable of ``EXCHANGE:TICKER`` strings.
        use_cache:   When False, bypass the 90s cache (e.g. for retries the
                     caller already coordinated, or to force fresh data).
        allow_stale: When the scanner is unavailable or rate-limited, return
                     the last good result (up to 4 bars / 6h old) as a
                     :class:`StaleAnalysis` instead of raising. Only for
                     callers that report ``stale_fields(result)``.

    Returns:
        Dict keyed by symbol → ``Analysis`` object (or ``None`` for misses).
        The values match ``tradingview_ta`` exactly so existing callers
        don't need to change how they read ``.indicators``.

    Raises:
        TVScannerRateLimited: 429s outlasted the budget, or the breaker is open.
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
        hit = _cache_get(key)
        if hit is not None:
            _log.debug("tv_scanner cache hit for %s/%s (%d symbols)", screener, interval, len(syms))
            return hit[1]

    try:
        result = _do_call(screener, interval, syms)
    except TVScannerUnavailable as exc:
        stale = _cache_get(key, max_age=_stale_limit(interval)) if allow_stale else None
        if stale is None:
            raise
        age, value = stale
        reason = getattr(exc, "reason", "unavailable")
        _log.warning("tv_scanner serving %.0fs-old cached %s/%s (%s)", age, screener, interval, reason)
        return StaleAnalysis(value, age_seconds=age, reason=reason)

    if not result:
        raise TVScannerEmpty(f"scanner returned no rows for {syms}")

    if use_cache:
        _cache_put(key, result)
    return result


# ── Response helpers ──────────────────────────────────────────────────────────

def upstream_fields(exc: BaseException) -> dict[str, Any]:
    """The ``upstream_status`` (+ retry hint) to report for a scanner failure:
    ``rate_limited`` for a 429 / open breaker, ``down`` for everything else."""
    if isinstance(exc, TVScannerRateLimited):
        out: dict[str, Any] = {"upstream_status": "rate_limited", "upstream_reason": exc.reason}
        if exc.retry_after is not None:
            out["retry_after_seconds"] = round(exc.retry_after)
        return out
    return {"upstream_status": "down"}


def stale_fields(analysis: dict[str, Any]) -> dict[str, Any]:
    """Fields a caller merges into its response when ``ta_call`` handed it a
    :class:`StaleAnalysis`; empty for a fresh result."""
    if not isinstance(analysis, StaleAnalysis):
        return {}
    return {
        "degraded": True,
        "data_source": "tradingview_cache",
        "upstream_status": "rate_limited" if analysis.reason in ("http_429", "circuit_open") else "down",
        "cache_age_seconds": round(analysis.age_seconds),
        "upstream_detail": (
            f"scanner.tradingview.com not answering ({analysis.reason}); "
            f"indicators are from a reading {round(analysis.age_seconds)}s old"
        ),
    }


def ta_call_or_error(
    screener: str,
    interval: str,
    symbols: Iterable[str],
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Like ``ta_call`` but never raises. Returns either:

    * ``{"_ok": True,  "analysis": {sym: Analysis, ...}}`` on success
    * ``{"_ok": False, "upstream_status": "down" | "rate_limited", "error": "...", "detail": ...}``
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
            **upstream_fields(e),
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
