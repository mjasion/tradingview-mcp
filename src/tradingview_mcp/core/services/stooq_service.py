"""Stooq.com price provider — fallback for tickers Yahoo Finance doesn't cover.

Stooq publishes a free, no-auth CSV endpoint for live quotes. Used here for
Warsaw Stock Exchange (GPW) tickers where Yahoo Finance returns null
(e.g. ``KGHM.WA``). Historical/range data requires an API key, so previous
close is derived from the same row's open price as a best-effort proxy
(a single-day candle) — the field is marked as approximate in the response.

Endpoint: https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcvn&h&e=csv
Format:   Symbol,Date,Time,Open,High,Low,Close,Volume,Name
"""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy

_TIMEOUT = 10
_UA = "tradingview-mcp/0.7.1"
_BASE = "https://stooq.com/q/l/"

# Yahoo Finance uses long-form Polish tickers (KGHM.WA, CDPROJEKT.WA) but Stooq
# uses the 3-letter GPW exchange codes (KGH, CDR). Map Yahoo → Stooq for the
# tickers most likely to appear via the .WA suffix path.
_YAHOO_TO_STOOQ_ALIASES: dict[str, str] = {
    "KGHM":      "kgh",
    "CDPROJEKT": "cdr",
    "PKNORLEN":  "pkn",
    "PKOBP":     "pko",
    "PEKAO":     "peo",
    "ASSECOPL":  "acp",
    "DINOPL":    "dnp",
    "ALLEGRO":   "ale",
    "ORANGEPL":  "opl",
    "PZU":       "pzu",
    "JSW":       "jsw",
    "CDR":       "cdr",
    "KGH":       "kgh",
}


def _normalize(symbol: str) -> str:
    """Map Yahoo/long-form ticker to Stooq's lowercase short code."""
    s = symbol.strip().upper()
    if s.endswith(".WA"):
        s = s[:-3]
    return _YAHOO_TO_STOOQ_ALIASES.get(s, s).lower()


def _fetch_csv(symbol: str) -> list[dict[str, str]]:
    url = f"{_BASE}?s={_normalize(symbol)}&f=sd2t2ohlcvn&h&e=csv"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        opener = build_opener_with_proxy(_UA)
        with opener.open(req, timeout=_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def _to_float(v: Optional[str]) -> Optional[float]:
    if v is None or v == "" or v == "N/D":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def get_price(symbol: str) -> dict:
    """Return Yahoo-Finance-shaped quote dict from Stooq.

    Stooq's free endpoint returns one row per query. Previous close is not
    available without an API key, so we fall back to the day's open price
    as an approximate previous close — flagged via ``previous_close_source``.
    Consumers that need a precise previous close should re-query against TV
    or maintain their own snapshot.
    """
    sym_upper = symbol.strip().upper()
    try:
        rows = _fetch_csv(symbol)
        if not rows:
            return {"symbol": sym_upper, "error": "Stooq returned no rows", "source": "Stooq"}
        row = rows[0]
        close = _to_float(row.get("Close"))
        open_ = _to_float(row.get("Open"))
        high = _to_float(row.get("High"))
        low = _to_float(row.get("Low"))
        volume = _to_float(row.get("Volume"))

        if close is None:
            return {
                "symbol": sym_upper,
                "error": f"Stooq has no quote for '{_normalize(symbol)}' (row: {row})",
                "source": "Stooq",
            }

        change = round(close - open_, 4) if open_ else None
        change_pct = round((close - open_) / open_ * 100, 2) if open_ else None

        return {
            "symbol":                 sym_upper,
            "price":                  close,
            "previous_close":         open_,
            "previous_close_source":  "intraday_open",
            "change":                 change,
            "change_pct":             change_pct,
            "open":                   open_,
            "high":                   high,
            "low":                    low,
            "volume":                 volume,
            "currency":               "PLN" if not _normalize(symbol).startswith(("us", "uk", "de")) else None,
            "exchange":               "GPW (Stooq)",
            "market_state":           "REGULAR" if row.get("Time") not in (None, "", "N/D") else "CLOSED",
            "name":                   row.get("Name") or None,
            "trade_date":             row.get("Date") or None,
            "trade_time":             row.get("Time") or None,
            "source":                 "Stooq",
            "timestamp":              datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"symbol": sym_upper, "error": str(e), "source": "Stooq"}
