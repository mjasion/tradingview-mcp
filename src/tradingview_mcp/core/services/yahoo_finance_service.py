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
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
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
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    _log.info("asking Yahoo Finance about %s (%s)", symbol.upper(), ", ".join(modules))
    if _CRUMB_CACHE["crumb"] is None:
        _log.debug("bootstrapping Yahoo crumb token")
        _CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"] = _bootstrap_crumb()

    try:
        data = _do_call(_CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"])
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _log.warning("Yahoo rejected our session (%s) — refreshing crumb and retrying", e.code)
            _CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"] = _bootstrap_crumb()
            data = _do_call(_CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"])
        else:
            _log.warning("Yahoo HTTP %s for %s", e.code, symbol)
            raise

    result = data.get("quoteSummary", {}).get("result") or []
    if not result:
        err = data.get("quoteSummary", {}).get("error", {}).get("description") or "no result"
        raise ValueError(f"empty quoteSummary for {symbol}: {err}")
    return result[0]


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
