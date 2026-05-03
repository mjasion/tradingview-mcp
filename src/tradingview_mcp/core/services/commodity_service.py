"""Commodity Snapshot — single-call composite for raw materials.

Why this exists: Polish portfolio reasoning ("powinienem dobrać KGHM?",
"czy JSW to dobry kierunek?") is dominated by underlying commodity trends.
KGHM tracks copper. JSW tracks coking coal / steel demand. Orlen tracks
crude. A single number per commodity with daily change + simple trend tag
is enough for Claude to ground a recommendation.

Strategy: pull spot/CFD quotes from TradingView via tradingview-ta. Two
calls because the working symbols span two screeners:
  * ``cfd`` screener — metals (TVC:GOLD/SILVER, OANDA:XCUUSD copper),
    nat-gas (OANDA:NATGASUSD), USD index (TVC:DXY)
  * ``america`` screener — oil via ETF proxies (AMEX:USO for WTI,
    AMEX:BNO for Brent). Pure-CFD oil symbols (TVC:USOIL, OANDA:WTICOUSD)
    return ``None`` from this free endpoint, so the ETFs are the only
    reliable signal. Note: ETF price ≠ oil spot, but daily-change
    correlation > 0.95, which is what Claude actually reasons over.

Coking coal has no clean public spot symbol — surfaced as ``null`` in
``notes`` rather than faked.

Output is intentionally compact (price + 24h change + RSI + trend tag) —
Claude formats the rest. Per-symbol failures degrade to ``None``; the
snapshot never raises.
"""
from __future__ import annotations

from typing import Optional

from tradingview_mcp.core.services.cache import cached
from tradingview_mcp.core.services.log import get_logger

_log = get_logger("commodity")

try:
    from tradingview_ta import get_multiple_analysis  # type: ignore
    _TA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TA_AVAILABLE = False


_TIMEFRAME = "1d"

# Display key → (TradingView symbol, screener).
# KGHM (copper) and Orlen (oil) drive most Polish-market questions.
# Steel/iron-ore proxies cover JSW (coking coal) thesis since the free TV
# endpoint exposes no clean spot symbol for those — only equity proxies work.
_COMMODITIES: dict[str, tuple[str, str]] = {
    "copper":     ("OANDA:XCUUSD",   "cfd"),       # KGHM proxy
    "gold":       ("TVC:GOLD",       "cfd"),       # safe-haven flow
    "silver":     ("TVC:SILVER",     "cfd"),       # industrial + monetary
    "natgas":     ("OANDA:NATGASUSD", "cfd"),      # Henry Hub
    "oil_wti":    ("AMEX:USO",       "america"),   # WTI ETF proxy
    "oil_brent":  ("AMEX:BNO",       "america"),   # Brent ETF proxy
    "dxy":        ("TVC:DXY",        "cfd"),       # USD index — context for all of the above
    "steel":      ("AMEX:SLX",       "america"),   # VanEck Steel ETF — JSW/coking-coal proxy
    "metals_mining": ("AMEX:XME",    "america"),   # SPDR Metals & Mining ETF — broad miner basket
    "iron_ore":   ("NYSE:VALE",      "america"),   # Vale equity — largest iron-ore exporter
    "us_steel":   ("NYSE:CLF",       "america"),   # Cleveland-Cliffs — flat-rolled steel + HRC pricing signal
}


def _classify_trend(rsi: Optional[float], ema50: Optional[float], close: Optional[float]) -> str:
    """Return a one-word trend tag from RSI + EMA50 position."""
    if rsi is None or ema50 is None or close is None:
        return "unknown"
    above_ema = close > ema50
    if rsi > 70:
        return "overbought" if above_ema else "rebound_overbought"
    if rsi < 30:
        return "oversold" if not above_ema else "pullback_oversold"
    return "uptrend" if above_ema else "downtrend"


def _row(ind: dict) -> dict:
    close = ind.get("close")
    open_ = ind.get("open")
    rsi = ind.get("RSI")
    ema50 = ind.get("EMA50")
    change_pct = None
    if close is not None and open_ not in (None, 0):
        change_pct = round(((close - open_) / open_) * 100, 2)
    return {
        "price": close,
        "change_24h_pct": change_pct,
        "rsi": round(rsi, 1) if isinstance(rsi, (int, float)) else None,
        "ema50": ema50,
        "trend": _classify_trend(rsi, ema50, close),
    }


@cached(ttl_seconds=900, namespace="commodity_snapshot")  # 15min
def get_commodity_snapshot() -> dict:
    """Bulk-fetch commodity dashboard. Never raises.

    Returns ``{tool, source, timeframe, commodities, notes}`` where each
    entry under ``commodities`` is either a populated dict or ``None`` if
    the upstream symbol returned no data.
    """
    out: dict = {
        "tool": "commodity_snapshot",
        "source": "TradingView (tradingview-ta)",
        "timeframe": _TIMEFRAME,
        "commodities": {k: None for k in _COMMODITIES},
        "notes": [
            "oil_wti / oil_brent are ETF proxies (AMEX:USO, AMEX:BNO). "
            "Daily-change correlation with spot crude > 0.95.",
            "steel / metals_mining / iron_ore / us_steel are equity proxies — "
            "no spot HRC or iron-ore symbol is exposed on the free TV endpoint. "
            "For JSW (coking coal): steel + us_steel are the closest signal.",
        ],
    }

    if not _TA_AVAILABLE:
        out["error"] = "tradingview_ta not installed"
        return out

    _log.info("building commodity dashboard (%d symbols)", len(_COMMODITIES))

    # Group by screener — one TA call per screener.
    by_screener: dict[str, list[tuple[str, str]]] = {}
    for label, (sym, screener) in _COMMODITIES.items():
        by_screener.setdefault(screener, []).append((label, sym))

    for screener, items in by_screener.items():
        symbols = [sym for _, sym in items]
        _log.debug("querying TradingView '%s' screener for %d symbols", screener, len(symbols))
        try:
            analysis = get_multiple_analysis(
                screener=screener, interval=_TIMEFRAME, symbols=symbols
            )
        except Exception as e:
            _log.warning("TradingView %s screener failed: %s", screener, e)
            out.setdefault("errors", []).append(
                f"{screener}: {type(e).__name__}: {e}"
            )
            continue

        for label, sym in items:
            data = analysis.get(sym) if analysis else None
            if not data or not getattr(data, "indicators", None):
                continue
            try:
                out["commodities"][label] = {"symbol": sym, **_row(data.indicators)}
            except Exception:
                continue

    populated = sum(1 for v in out["commodities"].values() if v)
    if not any(out["commodities"].values()):
        out["error"] = "all commodity symbols returned no data"
        _log.warning("commodity dashboard: 0/%d symbols returned data", len(out["commodities"]))
    else:
        _log.info("commodity dashboard: %d/%d symbols populated", populated, len(out["commodities"]))
    return out
