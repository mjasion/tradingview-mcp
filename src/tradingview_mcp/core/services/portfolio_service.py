"""Portfolio / watchlist scan — orchestration over existing services.

Why this exists: a "should I add to my positions?" question normally needs
4×N tool calls (TA, earnings, dividends, news per ticker). This tool batches
the same lookups and returns one compact dict with red-flag flags per symbol,
so Claude can cite "AAPL: RSI 78 (overbought), earnings in 3 days, ex-div in
9 days" in a single round-trip.

No new data sources. All calls go to existing services:
  * ``screener_service.analyze_coin`` — TA (RSI, BB, ATR, change %)
  * ``yahoo_finance_service.get_earnings`` — next earnings date
  * ``yahoo_finance_service.get_dividends`` — next ex-dividend
  * ``news_service.fetch_news_summary`` — recent news count
  * ``sec_service.get_insider_transactions`` — Form 4 count (US only)

Per-symbol fan-out runs in a thread pool — each call is I/O-bound. Results
that fail (rate-limit, unknown ticker) appear as ``error`` on that symbol's
sub-dict and do NOT block the rest of the scan.
"""
from __future__ import annotations

import collections
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from tradingview_mcp.core.services.log import get_logger
from tradingview_mcp.core.services.news_service import fetch_news_summary
from tradingview_mcp.core.services.screener_service import analyze_coin
from tradingview_mcp.core.services.sec_service import get_insider_transactions
from tradingview_mcp.core.services.yahoo_finance_service import (
    get_dividends,
    get_earnings,
)

_log = get_logger("scan")


# 4 keeps Yahoo's quoteSummary endpoint under its 429 threshold while still
# overlapping I/O. The semaphore inside yahoo_finance_service caps actual
# Yahoo concurrency at 3 — anything higher here just adds queueing without
# speeding up the scan.
_DEFAULT_WORKERS = 4
_RSI_OVERBOUGHT = 70.0
_RSI_OVERSOLD = 30.0
_EARNINGS_HORIZON_DAYS = 7
_EX_DIV_HORIZON_DAYS = 14


def _flags(ta: dict, earnings: dict, dividends: dict, news_count: int) -> list[str]:
    flags: list[str] = []
    rsi_val = (ta.get("rsi") or {}).get("value")
    if isinstance(rsi_val, (int, float)):
        if rsi_val >= _RSI_OVERBOUGHT:
            flags.append(f"rsi_overbought({rsi_val:.0f})")
        elif rsi_val <= _RSI_OVERSOLD:
            flags.append(f"rsi_oversold({rsi_val:.0f})")

    bb_pos = (ta.get("bollinger_bands") or {}).get("position")
    if bb_pos == "Above Upper Band":
        flags.append("bb_above_upper")
    elif bb_pos == "Below Lower Band":
        flags.append("bb_below_lower")

    vol = (ta.get("atr") or {}).get("volatility")
    if vol == "High":
        flags.append("volatility_high")

    days_until = earnings.get("days_until")
    if isinstance(days_until, int) and 0 <= days_until <= _EARNINGS_HORIZON_DAYS:
        flags.append(f"earnings_in_{days_until}d")

    next_ex = dividends.get("next_ex_date")
    if isinstance(next_ex, str):
        try:
            d = datetime.fromisoformat(next_ex).date()
            today = datetime.now(timezone.utc).date()
            delta = (d - today).days
            if 0 <= delta <= _EX_DIV_HORIZON_DAYS:
                flags.append(f"ex_dividend_in_{delta}d")
        except ValueError:
            pass

    if news_count >= 5:
        flags.append(f"news_active({news_count})")

    return flags


def _scan_one(
    symbol: str,
    exchange: str,
    timeframe: str,
    news_category: str,
    include_insider: bool,
) -> dict:
    """Run all per-symbol lookups serially inside a worker thread."""
    out: dict = {"symbol": symbol.upper()}
    started = time.perf_counter()
    _log.info("  ↪ %s: starting (TA + earnings + dividends + news)", symbol.upper())

    ta = analyze_coin(symbol, exchange, timeframe)
    if "error" in ta:
        out["ta_error"] = ta["error"]
        ta = {}
    else:
        out["price"] = (ta.get("price_data") or {}).get("current_price")
        out["change_pct"] = (ta.get("price_data") or {}).get("change_percent")
        out["rsi"] = (ta.get("rsi") or {}).get("value")
        out["volatility"] = (ta.get("atr") or {}).get("volatility")
        out["rating"] = (ta.get("market_sentiment") or {}).get("overall_rating")
        # Propagate degradation markers so the summary can count them and
        # downstream consumers know this is a fallback-derived data point.
        if ta.get("degraded"):
            out["degraded"] = True
        if ta.get("data_source"):
            out["data_source"] = ta["data_source"]
        if ta.get("upstream_status"):
            out["upstream_status"] = ta["upstream_status"]

    earnings = get_earnings(symbol)
    if "error" not in earnings:
        out["next_earnings_date"] = earnings.get("next_earnings_date")
        out["earnings_days_until"] = earnings.get("days_until")
    else:
        out["earnings_error"] = earnings["error"]
        # Propagate Yahoo's retry hint up to the caller. The summary
        # aggregator uses this to surface "Yahoo throttled, wait ~Ns" once
        # for the whole scan rather than per-symbol.
        if earnings.get("upstream_status") == "rate_limited":
            out["earnings_retry_after"] = earnings.get("retry_after_seconds")

    dividends = get_dividends(symbol)
    if "error" not in dividends:
        out["dividend_yield"] = dividends.get("dividend_yield")
        out["next_ex_date"] = dividends.get("next_ex_date")
    else:
        out["dividend_error"] = dividends["error"]
        if dividends.get("upstream_status") == "rate_limited":
            out["dividend_retry_after"] = dividends.get("retry_after_seconds")

    news_count = 0
    try:
        news = fetch_news_summary(category=news_category, symbol=symbol, limit=20)
        if isinstance(news, dict):
            news_count = int(news.get("count") or len(news.get("items") or []))
            out["news_count"] = news_count
    except Exception as e:
        out["news_error"] = f"{type(e).__name__}: {e}"

    if include_insider:
        ins = get_insider_transactions(symbol, limit=5)
        if "error" not in ins:
            out["insider_form4_count"] = ins.get("count")
            out["insider_recent"] = [f["date"] for f in (ins.get("filings") or [])[:3]]

    out["flags"] = _flags(ta, earnings if "error" not in earnings else {},
                          dividends if "error" not in dividends else {},
                          news_count)
    elapsed = (time.perf_counter() - started) * 1000
    if out["flags"]:
        _log.info("  ↪ %s: done in %dms — flags: %s",
                  symbol.upper(), elapsed, ", ".join(out["flags"]))
    else:
        _log.info("  ↪ %s: done in %dms — no flags", symbol.upper(), elapsed)
    return out


def portfolio_scan(
    symbols: list[str],
    exchange: str = "NASDAQ",
    timeframe: str = "1D",
    news_category: str = "stocks",
    include_insider: bool = False,
    max_workers: int = _DEFAULT_WORKERS,
) -> dict:
    """Batch-scan a watchlist. Returns ``{results: [...], summary, source}``.

    ``flags`` per symbol surface the things you actually care about:
      * ``rsi_overbought`` / ``rsi_oversold``
      * ``bb_above_upper`` / ``bb_below_lower``
      * ``volatility_high``
      * ``earnings_in_<N>d`` (only when 0 ≤ N ≤ 7)
      * ``ex_dividend_in_<N>d`` (only when 0 ≤ N ≤ 14)
      * ``news_active(<count>)``

    ``include_insider=True`` adds Form 4 counts (US-only, slow). Off by default.
    """
    if not symbols:
        return {"results": [], "summary": {"scanned": 0}, "source": "portfolio_scan"}

    symbols = [s.strip().upper() for s in symbols if s and s.strip()]
    workers = max(1, min(max_workers, len(symbols)))
    started = time.perf_counter()
    _log.info("portfolio_scan: %d symbols on %s — %s",
              len(symbols), exchange, ", ".join(symbols[:6]) + (" …" if len(symbols) > 6 else ""))
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_scan_one, sym, exchange, timeframe, news_category, include_insider): sym
            for sym in symbols
        }
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"symbol": sym, "error": f"{type(e).__name__}: {e}", "flags": []})

    # Stable, deterministic order (input order)
    by_sym = {r["symbol"]: r for r in results}
    ordered = [by_sym[s] for s in symbols if s in by_sym]

    flagged = [r for r in ordered if r.get("flags")]
    summary = {
        "scanned": len(ordered),
        "with_flags": len(flagged),
        "errors": sum(1 for r in ordered if "error" in r or "ta_error" in r),
    }

    # Degradation hint: when most TA errors share a root cause, that's an
    # upstream outage — not a bunch of bad tickers. Surface it loudly so the
    # caller (a model coordinating the scan) stops retrying the same way.
    ta_errors = [str(r["ta_error"]) for r in ordered if r.get("ta_error")]
    if ta_errors and len(ta_errors) >= max(2, int(len(ordered) * 0.5)):
        top_msg, top_count = collections.Counter(ta_errors).most_common(1)[0]
        is_tv_outage = "tradingview_scanner_unavailable" in top_msg
        summary["upstream_warning"] = {
            "ta_failure_ratio": round(len(ta_errors) / len(ordered), 2),
            "most_common_error": top_msg,
            "affected_count": top_count,
            "likely_cause": "tradingview_scanner_outage" if is_tv_outage else "shared_ta_failure",
            "recommended_action": (
                "scanner.tradingview.com is degraded. Wait 60-120s and retry, "
                "or rely on coin_analysis individually — it auto-falls back to Yahoo."
                if is_tv_outage else
                "Inspect the ta_error field on individual results; the same "
                "error across many symbols usually points at an upstream issue."
            ),
        }

    # Count results that came from the Yahoo fallback so the caller can
    # contextualize numbers as "degraded but real" vs missing.
    degraded = sum(1 for r in ordered if r.get("degraded") or r.get("data_source") == "yahoo_fallback")
    if degraded:
        summary["degraded_count"] = degraded

    # Yahoo rate-limit aggregation: when many rows surface a retry_after,
    # tell the caller once with the worst delay so it knows "wait N seconds
    # before re-running, don't immediately retry symbol-by-symbol".
    yahoo_throttled = [
        r for r in ordered
        if r.get("earnings_retry_after") is not None or r.get("dividend_retry_after") is not None
    ]
    if yahoo_throttled:
        worst_retry = max(
            max(r.get("earnings_retry_after") or 0, r.get("dividend_retry_after") or 0)
            for r in yahoo_throttled
        )
        summary["yahoo_rate_limit"] = {
            "affected_symbols": len(yahoo_throttled),
            "retry_after_seconds": int(worst_retry),
            "recommended_action": (
                f"Yahoo Finance is rate-limiting our IP (earnings/dividends "
                f"for {len(yahoo_throttled)} symbols missing). Wait ~{int(worst_retry)}s "
                f"before re-running portfolio_scan; TA + news data are still fresh."
            ),
        }

    elapsed = time.perf_counter() - started
    _log.info("portfolio_scan done in %.1fs — %d flagged, %d errored, %d degraded",
              elapsed, summary["with_flags"], summary["errors"], degraded)

    return {
        "results": ordered,
        "summary": summary,
        "source": "portfolio_scan",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
