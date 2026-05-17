"""
Yahoo Finance Price Service via Webshare Rotating Proxy.

Provides real-time quotes for stocks, ETFs, crypto pairs, indices
using the Yahoo Finance Chart API (no API key required).

Works with any symbol Yahoo Finance supports:
  Stocks:  AAPL, TSLA, MSFT, NVDA, GOOGL
  Crypto:  BTC-USD, ETH-USD, SOL-USD, BNB-USD
  ETFs:    SPY, QQQ, VTI
  Indices: ^GSPC (S&P500), ^DJI (Dow), ^IXIC (NASDAQ)
  FX:      EURUSD=X, GBPUSD=X
  Turkish: THYAO.IS, SASA.IS
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from tradingview_mcp.core.services.cache import cached
from tradingview_mcp.core.services.log import get_logger
from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy

_log = get_logger("yahoo")

_TIMEOUT = 12
_UA = "tradingview-mcp/0.5.0"
_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_QUOTE_SUMMARY_BASE = "https://query1.finance.yahoo.com/v10/finance/quoteSummary"

# Yahoo's /v10/quoteSummary endpoint silently 429s when too many concurrent
# requests share an IP. portfolio_scan used to fan out 6+ workers in parallel,
# each making 2 separate quoteSummary calls (earnings + dividends) — that hit
# the threshold reliably. We cap concurrency at 2 here so the rest queue up
# briefly instead of being rejected. The bound is process-wide so all callers
# (portfolio_scan + ad-hoc next_earnings + dividend_history) share the budget.
# NOTE: deliberately 1, not 2. Real-world traces show that even sequential
# tool calls (next_earnings → next_earnings → dividend_history, seconds
# apart) all 429 against Yahoo — meaning the rate-limit budget is far smaller
# than "a couple in flight". Pure sequential through quoteSummary lets the
# global cooldown gate below soak up 429s for the entire process. Other
# endpoints (chart/price) are unaffected.
_QUOTE_SUMMARY_SEM = threading.BoundedSemaphore(1)

# Process-wide cooldown: when ANY worker observes a 429, every other worker
# pauses until the Retry-After window expires. Without this gate, three
# parallel workers all see 429 at the same time and each sleeps independently
# — when they wake up they hit Yahoo simultaneously again and get re-banned.
# A shared cooldown timestamp lets the first 429 absorb the burst for everyone.
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_COOLDOWN_UNTIL = 0.0  # time.monotonic()-based


class YahooRateLimited(RuntimeError):
    """Yahoo returned HTTP 429 after we already retried once.

    Carries the suggested retry delay so callers (and ultimately the MCP
    response) can tell the model how long to wait before trying again,
    instead of swallowing the rate limit as a generic 'HTTPError 429'.
    """

    def __init__(self, retry_after_seconds: float, detail: str = ""):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(detail or f"Yahoo rate-limited; retry in {retry_after_seconds:.0f}s")


def _wait_for_rate_limit_window() -> None:
    """Block until any active cooldown set by a previous 429 has expired."""
    while True:
        with _RATE_LIMIT_LOCK:
            remaining = _RATE_LIMIT_COOLDOWN_UNTIL - time.monotonic()
            if remaining <= 0:
                return
        # Sleep in short slices so a longer cooldown overrides without us
        # holding the lock across the wait.
        time.sleep(min(remaining, 1.0))


def _record_rate_limit(retry_after_seconds: float) -> None:
    """Extend the process-wide cooldown so other workers also back off."""
    global _RATE_LIMIT_COOLDOWN_UNTIL
    with _RATE_LIMIT_LOCK:
        target = time.monotonic() + retry_after_seconds
        if target > _RATE_LIMIT_COOLDOWN_UNTIL:
            _RATE_LIMIT_COOLDOWN_UNTIL = target


def _parse_retry_after(header_value: Optional[str], default: float = 8.0) -> float:
    """Convert a Retry-After header value to a sane delay in seconds."""
    if not header_value:
        return default
    try:
        return max(1.0, min(60.0, float(header_value)))
    except (TypeError, ValueError):
        return default


# ── Circuit breaker for persistent 429s ───────────────────────────────────────
#
# When we have NO proxy and Yahoo's per-IP daily quota is hit, no amount of
# Retry-After backoff helps — every call burns time waiting + a request, only
# to get 429 again. The circuit breaker trips after one persistent failure
# (a 429 that survived our in-line retry) and stays open for ``_BREAKER_OPEN_SECONDS``.
# While open, every quoteSummary caller short-circuits to a rate_limited
# response WITHOUT hitting Yahoo. That:
#   * makes portfolio_scan return in ~ms instead of minutes when Yahoo is down,
#   * stops adding to the bad-actor score that may extend the block,
#   * gives the LLM one clear "wait N minutes" hint instead of N opaque errors.
#
# After the window expires, the breaker half-opens: the next call IS sent. If
# it succeeds, the breaker resets; if it 429s again, the window is renewed.
_BREAKER_OPEN_SECONDS = 300.0  # 5 minutes — long enough to outlast a transient
                                # rolling-window block, short enough that a
                                # daily quota reset isn't far behind.
_BREAKER_OPEN_UNTIL = 0.0


def _breaker_is_open() -> tuple[bool, float]:
    """Return (open?, seconds_remaining)."""
    with _RATE_LIMIT_LOCK:
        remaining = _BREAKER_OPEN_UNTIL - time.monotonic()
        return (remaining > 0, max(0.0, remaining))


def _trip_breaker(seconds: float = _BREAKER_OPEN_SECONDS) -> None:
    """Open the breaker for ``seconds`` seconds, extending any existing window."""
    global _BREAKER_OPEN_UNTIL
    with _RATE_LIMIT_LOCK:
        target = time.monotonic() + seconds
        if target > _BREAKER_OPEN_UNTIL:
            _BREAKER_OPEN_UNTIL = target


def _reset_breaker() -> None:
    """Close the breaker after a successful Yahoo call."""
    global _BREAKER_OPEN_UNTIL
    with _RATE_LIMIT_LOCK:
        _BREAKER_OPEN_UNTIL = 0.0

# Yahoo's v10/quoteSummary endpoint requires a session crumb since 2023.
# We cache the (cookies, crumb) tuple per process — a single bootstrap is
# enough for the lifetime of an MCP server invocation. None means "not yet
# fetched" or "fetch failed; try again next call".
_CRUMB_CACHE: dict = {"crumb": None, "cookies": None}
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _fetch_quote(symbol: str) -> dict:
    """Fetch raw Yahoo Finance chart result for a symbol (meta + indicators)."""
    url = f"{_BASE}/{symbol}?interval=1d&range=2d"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    opener = build_opener_with_proxy(_UA)
    with opener.open(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["chart"]["result"][0]


def _get_previous_close(chart_result: dict) -> Optional[float]:
    """Extract previous trading day's close from candle data.

    The meta fields 'previousClose' and 'chartPreviousClose' are unreliable:
    - 'previousClose' is often None
    - 'chartPreviousClose' returns the chart range start price, not yesterday's close

    Instead, we use the actual close prices from the 2-day candle data.
    With range=2d, indicators.quote[0].close gives [prev_day_close, today_close].
    """
    try:
        closes = chart_result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        # Filter out None values (can happen for incomplete candles)
        valid_closes = [c for c in closes if c is not None]
        if len(valid_closes) >= 2:
            return valid_closes[-2]
    except (IndexError, TypeError, KeyError):
        pass
    # Fallback to meta fields if candle data unavailable
    meta = chart_result.get("meta", {})
    return meta.get("previousClose") or meta.get("chartPreviousClose")


def get_price(symbol: str) -> dict:
    """
    Get real-time price data for any Yahoo Finance symbol.

    Args:
        symbol: Yahoo Finance symbol (e.g. "AAPL", "BTC-USD", "THYAO.IS", "^GSPC")

    Returns:
        dict with price, change, change_pct, currency, exchange, market_state
    """
    try:
        chart_result = _fetch_quote(symbol)
        meta = chart_result.get("meta", {})
        price      = meta.get("regularMarketPrice")
        prev_close = _get_previous_close(chart_result) or price
        chg        = round(price - prev_close, 4) if (price and prev_close) else None
        chg_pct    = round((price - prev_close) / prev_close * 100, 2) if (price and prev_close and prev_close != 0) else None

        return {
            "symbol":        symbol.upper(),
            "price":         price,
            "previous_close": prev_close,
            "change":        chg,
            "change_pct":    chg_pct,
            "currency":      meta.get("currency", "USD"),
            "exchange":      meta.get("exchangeName", ""),
            "market_state":  meta.get("marketState", ""),  # REGULAR, PRE, POST, CLOSED
            "52w_high":      meta.get("fiftyTwoWeekHigh"),
            "52w_low":       meta.get("fiftyTwoWeekLow"),
            "source":        "Yahoo Finance",
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"symbol": symbol.upper(), "error": str(e), "source": "Yahoo Finance"}


def get_prices_bulk(symbols: list[str]) -> list[dict]:
    """
    Get prices for multiple symbols at once.

    Args:
        symbols: List of Yahoo Finance symbols

    Returns:
        List of price dicts
    """
    results = []
    for sym in symbols:
        results.append(get_price(sym))
    return results


def _bootstrap_crumb() -> tuple[str, str]:
    """Obtain a (crumb, cookie-header) pair for v10/quoteSummary calls.

    Hit fc.yahoo.com to set consent cookies, then v1/test/getcrumb to receive
    a crumb token. Both need the same cookie jar, so we use a CookieJar-backed
    opener and replay the Set-Cookie headers as a Cookie header on subsequent
    requests (urllib's stdlib doesn't auto-replay across openers).
    """
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    # Route bootstrap through the Webshare proxy too. Without this, the crumb
    # session is anchored to the container's outbound IP — and that's exactly
    # the IP Yahoo blacklisted, so every subsequent quoteSummary 429s. Sharing
    # the proxy means the bootstrap, the crumb, and the quoteSummary call all
    # originate from the same rotating proxy egress.
    opener = build_opener_with_proxy(
        _BROWSER_UA, extra_handlers=(urllib.request.HTTPCookieProcessor(jar),)
    )
    opener.addheaders = [
        ("User-Agent", _BROWSER_UA),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]
    # 1. Land on a Yahoo Finance page to populate cookies.
    try:
        opener.open("https://fc.yahoo.com", timeout=_TIMEOUT).read(0)
    except Exception:
        pass  # fc.yahoo.com may 4xx; cookies still set
    # 2. Pick up the crumb token.
    with opener.open(
        "https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=_TIMEOUT
    ) as resp:
        crumb = resp.read().decode("utf-8").strip()
    if not crumb or "<" in crumb:
        raise ValueError(f"empty crumb (got {crumb!r})")
    cookie_header = "; ".join(f"{c.name}={c.value}" for c in jar)
    return crumb, cookie_header


def _fetch_quote_summary(symbol: str, modules: list[str]) -> dict:
    """Fetch /v10/finance/quoteSummary modules for *symbol*. Returns module dict.

    Yahoo's v10 endpoint requires both a browser-like UA and a crumb token
    bootstrapped from fc.yahoo.com cookies. The crumb is cached per process;
    on 401/403 we drop the cache and retry once.
    """
    def _do_call(crumb: str, cookie_header: str) -> dict:
        url = f"{_QUOTE_SUMMARY_BASE}/{symbol}?modules={','.join(modules)}&crumb={crumb}"
        req = urllib.request.Request(url, headers={
            "User-Agent": _BROWSER_UA,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": cookie_header,
        })
        # Go through the Webshare proxy — the chart endpoint already does
        # this and stays healthy; quoteSummary was the only Yahoo path
        # still leaking the container's outbound IP and getting 429'd.
        opener = build_opener_with_proxy(_BROWSER_UA)
        with opener.open(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    _log.info("asking Yahoo Finance about %s (%s)", symbol.upper(), ", ".join(modules))

    # Circuit breaker: if a previous call confirmed Yahoo is rate-limiting this
    # IP, don't even send the request. Return the same typed exception so the
    # caller renders the structured rate_limited envelope without waiting.
    open_, remaining = _breaker_is_open()
    if open_:
        _log.warning(
            "Yahoo circuit-breaker OPEN — skipping call for %s (%.0fs remaining)",
            symbol, remaining,
        )
        raise YahooRateLimited(
            retry_after_seconds=remaining,
            detail=(
                f"Yahoo Finance circuit-breaker open ({remaining:.0f}s remaining) — "
                f"recent persistent 429s. Wait for the breaker to close before retrying."
            ),
        )

    if _CRUMB_CACHE["crumb"] is None:
        _log.debug("bootstrapping Yahoo crumb token")
        _CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"] = _bootstrap_crumb()

    # First wait out any active cooldown set by a previous 429 anywhere in
    # the process. Doing this BEFORE we grab the semaphore means we don't
    # hold the (2-slot) budget while idle — other unrelated callers can still
    # make progress if the cooldown is short.
    _wait_for_rate_limit_window()

    # Serialize through the semaphore so parallel callers don't trip the 429
    # threshold. The wait is normally sub-second; we don't add a timeout because
    # callers (portfolio_scan) already cap symbol count.
    with _QUOTE_SUMMARY_SEM:
        try:
            data = _do_call(_CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"])
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                _log.warning("Yahoo rejected our session (%s) — refreshing crumb and retrying", e.code)
                _CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"] = _bootstrap_crumb()
                data = _do_call(_CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"])
            elif e.code == 429:
                # Respect Retry-After when Yahoo sends it; otherwise back off
                # for a few seconds. Record the cooldown globally so other
                # workers don't keep banging on the door while we wait. One
                # retry — beyond that the caller gets a typed YahooRateLimited
                # with the retry hint to pass up to the MCP response.
                retry_after_hdr = e.headers.get("Retry-After") if e.headers else None
                delay = _parse_retry_after(retry_after_hdr, default=8.0)
                _record_rate_limit(delay)
                _log.warning(
                    "Yahoo 429 for %s — Retry-After=%s, backing off %.1fs (process-wide cooldown set) and retrying once",
                    symbol, retry_after_hdr or "<none>", delay,
                )
                time.sleep(delay)
                try:
                    data = _do_call(_CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"])
                except urllib.error.HTTPError as e2:
                    if e2.code == 429:
                        # Still rate-limited. This is the trigger for the
                        # circuit breaker: a retry-after-wait still 429s means
                        # we're not just bursting, we're banned. Trip the
                        # breaker so subsequent calls skip Yahoo entirely.
                        second_hdr = e2.headers.get("Retry-After") if e2.headers else None
                        second_delay = _parse_retry_after(second_hdr, default=delay * 2)
                        _record_rate_limit(second_delay)
                        _trip_breaker()
                        _log.warning(
                            "Yahoo persistent 429 for %s — opening circuit breaker for %.0fs",
                            symbol, _BREAKER_OPEN_SECONDS,
                        )
                        raise YahooRateLimited(
                            retry_after_seconds=max(second_delay, _BREAKER_OPEN_SECONDS),
                            detail=(
                                f"Yahoo Finance rate limit persisted after one retry for {symbol}. "
                                f"Circuit breaker opened for {_BREAKER_OPEN_SECONDS:.0f}s; "
                                f"subsequent calls will short-circuit until it closes."
                            ),
                        ) from e2
                    raise
            else:
                _log.warning("Yahoo HTTP %s for %s", e.code, symbol)
                raise

    result = data.get("quoteSummary", {}).get("result") or []
    if not result:
        err = data.get("quoteSummary", {}).get("error", {}).get("description") or "no result"
        raise ValueError(f"empty quoteSummary for {symbol}: {err}")

    # A successful call closes the breaker — Yahoo is responding again.
    _reset_breaker()
    return result[0]


def _rate_limited_response(base: dict, exc: YahooRateLimited) -> dict:
    """Translate a YahooRateLimited into the structured envelope every tool
    returns when the upstream is throttling us.

    The shape mirrors the ``upstream_status: "down"`` envelope from
    ``tv_scanner.ta_call_or_error`` so the MCP caller can branch on a single
    field. ``retry_after_seconds`` is the value Yahoo asked us to wait
    (parsed from the Retry-After header when present, otherwise a sane
    default). Without this, callers got an opaque ``HTTPError 429`` string
    and couldn't tell rate-limiting apart from a permanently dead symbol.
    """
    retry_in = max(1.0, exc.retry_after_seconds)
    breaker_open, _ = _breaker_is_open()
    if breaker_open:
        hint = (
            f"Yahoo Finance is throttling this IP. Circuit breaker open for "
            f"~{int(round(retry_in))}s — subsequent next_earnings / dividend_history "
            f"calls will short-circuit with this status (no request sent) until the "
            f"breaker closes. TradingView TA, chart-based price data, Stooq and news "
            f"endpoints are unaffected."
        )
    else:
        hint = (
            f"Yahoo Finance is rate-limiting our IP — retry this symbol in "
            f"~{int(round(retry_in))}s. Other tools (TradingView TA, Stooq, news) "
            f"are unaffected."
        )
    return {
        **base,
        "error": "yahoo_rate_limited",
        "upstream_status": "rate_limited",
        "retry_after_seconds": int(round(retry_in)),
        "circuit_breaker_open": breaker_open,
        "retry_hint": hint,
        "detail": str(exc),
    }


def _ts_to_iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (ValueError, OSError, TypeError):
        return None


@cached(ttl_seconds=21600, namespace="yahoo_earnings")  # 6h
def get_earnings(symbol: str) -> dict:
    """Earnings calendar + recent surprise history for *symbol*.

    Returns ``{symbol, next_earnings_date, days_until, history, source}``
    where ``history`` is a list of recent EPS-surprise dicts. On any
    upstream failure returns an ``error`` field — never raises.
    """
    out: dict = {"symbol": symbol.upper(), "source": "Yahoo Finance"}
    try:
        block = _fetch_quote_summary(symbol, ["calendarEvents", "earningsHistory"])
    except YahooRateLimited as e:
        return _rate_limited_response(out, e)
    except Exception as e:
        return {**out, "error": f"{type(e).__name__}: {e}"}

    cal = block.get("calendarEvents", {}).get("earnings", {}) or {}
    raw_dates = cal.get("earningsDate") or []
    next_date_iso = None
    days_until = None
    for d in raw_dates:
        ts = d.get("raw") if isinstance(d, dict) else d
        next_date_iso = _ts_to_iso(ts)
        if next_date_iso:
            try:
                delta = (
                    datetime.fromisoformat(next_date_iso).date()
                    - datetime.now(timezone.utc).date()
                )
                days_until = delta.days
            except ValueError:
                days_until = None
            break

    history: list[dict] = []
    for entry in (block.get("earningsHistory", {}).get("history") or [])[-4:]:
        history.append({
            "quarter":      entry.get("quarter", {}).get("fmt"),
            "eps_actual":   entry.get("epsActual", {}).get("raw"),
            "eps_estimate": entry.get("epsEstimate", {}).get("raw"),
            "surprise_pct": entry.get("surprisePercent", {}).get("raw"),
        })

    return {
        **out,
        "next_earnings_date": next_date_iso,
        "days_until": days_until,
        "history": history,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@cached(ttl_seconds=86400, namespace="yahoo_dividends")  # 24h
def get_dividends(symbol: str) -> dict:
    """Forward dividend metrics + ex-date for *symbol*.

    Returns ``{symbol, dividend_yield, ex_dividend_date, payout_ratio,
    five_year_avg_yield, last_annual_dividend, source}``.
    """
    out: dict = {"symbol": symbol.upper(), "source": "Yahoo Finance"}
    try:
        block = _fetch_quote_summary(
            symbol, ["summaryDetail", "calendarEvents", "defaultKeyStatistics"]
        )
    except YahooRateLimited as e:
        return _rate_limited_response(out, e)
    except Exception as e:
        return {**out, "error": f"{type(e).__name__}: {e}"}

    sd = block.get("summaryDetail", {}) or {}
    keys = block.get("defaultKeyStatistics", {}) or {}
    cal = block.get("calendarEvents", {}) or {}

    def _raw(d: dict, k: str) -> Optional[float]:
        v = d.get(k)
        return v.get("raw") if isinstance(v, dict) else None

    return {
        **out,
        "dividend_yield":         _raw(sd, "dividendYield"),
        "trailing_annual_yield":  _raw(sd, "trailingAnnualDividendYield"),
        "trailing_annual_rate":   _raw(sd, "trailingAnnualDividendRate"),
        "five_year_avg_yield":    _raw(sd, "fiveYearAvgDividendYield"),
        "payout_ratio":           _raw(sd, "payoutRatio"),
        "ex_dividend_date":       _ts_to_iso(_raw(sd, "exDividendDate")),
        "next_ex_date":           _ts_to_iso(_raw(cal, "exDividendDate")),
        "next_dividend_date":     _ts_to_iso(_raw(cal, "dividendDate")),
        "last_dividend_value":    _raw(keys, "lastDividendValue"),
        "last_dividend_date":     _ts_to_iso(_raw(keys, "lastDividendDate")),
        "timestamp":              datetime.now(timezone.utc).isoformat(),
    }


def get_market_snapshot() -> dict:
    """
    Get a snapshot of major market indices and crypto prices.

    Returns:
        Dict with stocks (S&P500, NASDAQ, Dow), crypto (BTC, ETH), and FX
    """
    groups = {
        "indices": ["^GSPC", "^DJI", "^IXIC", "^VIX"],
        "crypto":  ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD"],
        "fx":      ["EURUSD=X", "GBPUSD=X", "JPYUSD=X"],
        "etfs":    ["SPY", "QQQ", "GLD"],
    }

    result = {}
    for group, syms in groups.items():
        result[group] = []
        for sym in syms:
            data = get_price(sym)
            if "error" not in data:
                result[group].append({
                    "symbol":     data["symbol"],
                    "price":      data["price"],
                    "change_pct": data["change_pct"],
                    "currency":   data["currency"],
                })

    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    return result
