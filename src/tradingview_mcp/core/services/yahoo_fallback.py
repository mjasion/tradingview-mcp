"""Yahoo Finance fallback for analyze_coin when TradingView scanner is down.

Why this exists: ``scanner.tradingview.com`` is a single point of failure
for every TA tool in this MCP (see ``tv_scanner.py``). When it returns
empty bodies (weekend maintenance, Cloudflare flap), all of ``coin_analysis``,
``combined_analysis``, ``portfolio_scan`` lose their RSI / MACD / BB numbers,
forcing the caller to pivot to text-based WebSearch with no hard indicators.

This module gives them an escape hatch: pull OHLCV candles from Yahoo
Finance (or Stooq for GPW where Yahoo is unreliable), compute the same
indicators locally with ``indicators_calc``, and assemble a dict that has
the **same shape** as a normal ``analyze_coin`` return so callers don't need
to special-case the fallback path.

Coverage:

* US stocks (NASDAQ/NYSE/AMEX), ETFs, indices → Yahoo direct
* Crypto (KUCOIN/BINANCE/MEXC ``BTCUSDT``) → mapped to Yahoo's ``BTC-USD`` form
* GPW / WSE → Stooq OHLC history (Yahoo's coverage of ``.WA`` is patchy)
* Global stocks (LSE, XETRA, TSE, etc.) → Yahoo with exchange-suffix mapping

What we deliberately *don't* compute (gracefully degraded to ``None``):

* TradingView's proprietary recommendation rating (no public formula)
* CCI, VWAP — would need full session OHLC, not worth the extra fetch
* Stochastic — best-effort from highs/lows of the lookback window

The output always carries ``"data_source": "yahoo_fallback"`` and
``"degraded": True`` so downstream UIs can flag the result as derived,
not authoritative.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from tradingview_mcp.core.services.indicators import (
    analyze_timeframe_context,
    compute_metrics,
    compute_stock_score,
    compute_trade_quality,
    compute_trade_setup,
    extract_extended_indicators,
)
from tradingview_mcp.core.services.indicators_calc import (
    calc_atr,
    calc_bollinger,
    calc_ema,
    calc_macd,
    calc_rsi,
    calc_sma,
)
from tradingview_mcp.core.services.log import get_logger
from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy
from tradingview_mcp.core.utils.validators import is_stock_exchange

_log = get_logger("yahoo_fallback")


# ── Endpoint config ───────────────────────────────────────────────────────────

_YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
_STOOQ_HIST = "https://stooq.com/q/d/l/"  # historical OHLCV CSV
_TIMEOUT = 12
_UA = "tradingview-mcp/0.7.0 yahoo-fallback"


# ── Interval mapping ──────────────────────────────────────────────────────────
# Yahoo supports: 1m, 2m, 5m, 15m, 30m, 60m/1h, 90m, 1d, 5d, 1wk, 1mo, 3mo.
# Short intervals are capped at ~60 days of history; daily is unlimited.
# We need ≥ 250 candles for SMA200 — pick range accordingly.

_TF_TO_YAHOO: dict[str, tuple[str, str]] = {
    "5m":  ("5m",  "5d"),
    "15m": ("15m", "1mo"),
    "1h":  ("60m", "3mo"),
    "4h":  ("60m", "6mo"),   # Yahoo has no native 4h — use 60m and downsample below
    "1D":  ("1d",  "2y"),
    "1W":  ("1wk", "5y"),
    "1M":  ("1mo", "10y"),
}


# ── Symbol mapping per exchange ───────────────────────────────────────────────

_CRYPTO_EXCHANGES = {"kucoin", "binance", "mexc", "bybit", "okx", "bitfinex", "huobi", "gate", "kraken"}
_GPW_EXCHANGES = {"gpw", "wse"}

# Append-suffix map for Yahoo (e.g. "XETRA:SAP" → "SAP.DE").
_YAHOO_SUFFIX: dict[str, str] = {
    # Europe
    "xetra": "DE", "xetr": "DE", "fwb": "DE", "fra": "F",
    "lse": "L", "lon": "L", "uk": "L",
    "euronext": "PA", "epa": "PA", "par": "PA",
    "ams": "AS", "ena": "AS",
    "ebr": "BR", "bru": "BR",
    "els": "LS", "lis": "LS",
    "mil": "MI", "borsa": "MI", "bit": "MI",
    "bme": "MC", "mce": "MC",
    "six": "SW", "swx": "SW", "ebs": "SW",
    "vie": "VI", "wbag": "VI",
    "osl": "OL", "ose": "OL",
    "omxsto": "ST", "sto": "ST",
    "omxcop": "CO", "cph": "CO",
    "omxhex": "HE", "hel": "HE",
    # Other regions
    "tsx": "TO", "tsxv": "V",
    "tse": "T", "tyo": "T", "jpx": "T",
    "krx": "KS", "kospi": "KS", "kosdaq": "KQ",
    "hkex": "HK", "hk": "HK",
    "asx": "AX",
    "bist": "IS",
    # US (no suffix needed but listed for completeness)
    "nasdaq": "", "nyse": "", "amex": "", "nysearca": "",
}


def _yahoo_symbol(symbol: str, exchange: str) -> Optional[str]:
    """Map (symbol, exchange) → Yahoo-style ticker; None when we shouldn't try."""
    ex = exchange.strip().lower()
    s = symbol.strip().upper()

    if ex in _CRYPTO_EXCHANGES:
        # KUCOIN:BTCUSDT → BTC-USD
        if s.endswith("USDT"):
            return f"{s[:-4]}-USD"
        if s.endswith("USDC") or s.endswith("BUSD"):
            return f"{s[:-4]}-USD"
        return None

    if ex in _GPW_EXCHANGES:
        # Yahoo's .WA coverage is patchy — we prefer Stooq for GPW.
        return None

    suffix = _YAHOO_SUFFIX.get(ex)
    if suffix is None:
        # Default: try the bare symbol; works for US exchanges and unmapped ones.
        return s
    return f"{s}.{suffix}" if suffix else s


# ── Yahoo OHLCV fetch ─────────────────────────────────────────────────────────

def _fetch_yahoo_ohlcv(symbol: str, interval: str, range_: str) -> list[dict]:
    url = f"{_YF_CHART}/{symbol}?interval={interval}&range={range_}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})

    data = None
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        pass

    if data is None:
        try:
            opener = build_opener_with_proxy(_UA)
            with opener.open(url, timeout=_TIMEOUT + 4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"yahoo fetch failed (direct + proxy): {e}") from e

    result = (data.get("chart") or {}).get("result") or []
    if not result:
        err = (data.get("chart") or {}).get("error") or {}
        raise RuntimeError(f"yahoo empty result for {symbol}: {err.get('description', 'no result')}")

    r = result[0]
    timestamps = r.get("timestamp") or []
    q = (r.get("indicators") or {}).get("quote") or [{}]
    q0 = q[0]
    out: list[dict] = []
    for i, ts in enumerate(timestamps):
        o, h, l, c, v = (
            (q0.get("open") or [None])[i],
            (q0.get("high") or [None])[i],
            (q0.get("low") or [None])[i],
            (q0.get("close") or [None])[i],
            (q0.get("volume") or [None])[i],
        )
        if None in (o, h, l, c):
            continue
        out.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v or 0})
    return out


# ── Stooq OHLCV fetch (GPW) ───────────────────────────────────────────────────

def _stooq_symbol(symbol: str) -> str:
    """Mirror of stooq_service._normalize, kept local to avoid coupling."""
    from tradingview_mcp.core.services.stooq_service import _normalize  # type: ignore
    return _normalize(symbol)


def _fetch_stooq_ohlcv(symbol: str, interval: str) -> list[dict]:
    """Fetch GPW OHLC history from Stooq CSV. Returns ≥200 most recent rows when possible."""
    stooq_sym = _stooq_symbol(symbol)
    # Stooq daily history endpoint: i=d (daily), w (weekly), m (monthly).
    interval_map = {"1D": "d", "1W": "w", "1M": "m"}
    i_code = interval_map.get(interval, "d")
    candidates = [stooq_sym, f"{stooq_sym}.pl"]
    last_err: Optional[Exception] = None
    for cand in candidates:
        url = f"{_STOOQ_HIST}?s={cand}&i={i_code}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            continue
        if "Date,Open,High,Low,Close,Volume" not in text:
            continue
        import csv
        rows = list(csv.DictReader(io.StringIO(text)))
        out: list[dict] = []
        for r in rows:
            try:
                out.append({
                    "ts": int(datetime.fromisoformat(r["Date"]).replace(
                        tzinfo=timezone.utc).timestamp()),
                    "open": float(r["Open"]),
                    "high": float(r["High"]),
                    "low":  float(r["Low"]),
                    "close": float(r["Close"]),
                    "volume": float(r["Volume"] or 0),
                })
            except (KeyError, ValueError):
                continue
        if out:
            return out
    raise RuntimeError(
        f"stooq history empty for {symbol} (tried {candidates}; last error: {last_err})"
    )


# ── Resampling (4h from 60m) ──────────────────────────────────────────────────

def _resample_to_4h(rows: list[dict]) -> list[dict]:
    """Aggregate hourly candles into 4-hour buckets aligned to UTC."""
    if not rows:
        return rows
    bucketed: dict[int, dict] = {}
    for r in rows:
        bucket = (r["ts"] // (4 * 3600)) * (4 * 3600)
        b = bucketed.get(bucket)
        if b is None:
            bucketed[bucket] = dict(r)
            bucketed[bucket]["ts"] = bucket
            continue
        b["high"] = max(b["high"], r["high"])
        b["low"] = min(b["low"], r["low"])
        b["close"] = r["close"]
        b["volume"] += r["volume"]
    return [bucketed[k] for k in sorted(bucketed)]


# ── Indicator panel ───────────────────────────────────────────────────────────

def _last(series: list[Optional[float]]) -> Optional[float]:
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _build_tv_shaped_indicators(rows: list[dict]) -> dict:
    """Compute indicators and return a TradingView-style flat dict.

    Mirrors the field names ``extract_extended_indicators`` reads:
    ``open/high/low/close/volume``, ``RSI``, ``SMA10/20/30/50/100/200``,
    ``EMA9/10/20/30/50/100/200``, ``MACD.macd/MACD.signal``,
    ``BB.upper/BB.lower``, ``ATR``, ``volume.SMA20``.
    """
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    opens = [r["open"] for r in rows]
    volumes = [r["volume"] for r in rows]

    last = rows[-1]
    out: dict = {
        "open":   last["open"],
        "high":   last["high"],
        "low":    last["low"],
        "close":  last["close"],
        "volume": last["volume"],
    }

    # SMAs (only compute the periods callers actually consume)
    for period in (10, 20, 30, 50, 100, 200):
        out[f"SMA{period}"] = _last(calc_sma(closes, period))
    # EMAs
    for period in (9, 10, 20, 30, 50, 100, 200):
        out[f"EMA{period}"] = _last(calc_ema(closes, period))

    out["RSI"] = _last(calc_rsi(closes, 14))

    macd = calc_macd(closes, 12, 26, 9)
    out["MACD.macd"] = _last(macd["macd"])
    out["MACD.signal"] = _last(macd["signal"])

    bb = calc_bollinger(closes, 20, 2.0)
    out["BB.upper"] = _last(bb["upper"])
    out["BB.lower"] = _last(bb["lower"])

    if len(highs) >= 15:
        out["ATR"] = _last(calc_atr(highs, lows, closes, 14))

    # volume.SMA20
    if len(volumes) >= 20:
        avg20 = sum(volumes[-20:]) / 20
        out["volume.SMA20"] = avg20

    # Pivot point classic — useful for support/resistance signals.
    if len(rows) >= 2:
        prev = rows[-2]
        pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
        out["Pivot.M.Classic.Middle"] = pivot
        out["Pivot.M.Classic.R1"] = 2 * pivot - prev["low"]
        out["Pivot.M.Classic.S1"] = 2 * pivot - prev["high"]
        out["Pivot.M.Classic.R2"] = pivot + (prev["high"] - prev["low"])
        out["Pivot.M.Classic.S2"] = pivot - (prev["high"] - prev["low"])

    # Best-effort Stochastic %K (14)
    if len(closes) >= 14:
        hi14 = max(highs[-14:])
        lo14 = min(lows[-14:])
        if hi14 > lo14:
            out["Stoch.K"] = (closes[-1] - lo14) / (hi14 - lo14) * 100

    return out


# ── Public entrypoint ─────────────────────────────────────────────────────────

def analyze_coin_yahoo_fallback(symbol: str, exchange: str, timeframe: str) -> dict:
    """Compute analyze_coin-shape dict from Yahoo / Stooq candles.

    Returns the same keys as ``screener_service.analyze_coin`` on success.
    Always adds ``data_source: "yahoo_fallback"`` so callers can tell where
    the numbers came from. On any failure returns ``{"error": "..."}``.
    """
    ex_lower = exchange.strip().lower()

    # 1. Decide source + symbol.
    yahoo_sym = _yahoo_symbol(symbol, exchange)
    source: str
    rows: list[dict]

    yh_interval = _TF_TO_YAHOO.get(timeframe, _TF_TO_YAHOO["1D"])

    try:
        if ex_lower in _GPW_EXCHANGES:
            source = "stooq"
            rows = _fetch_stooq_ohlcv(symbol, timeframe)
        else:
            if yahoo_sym is None:
                return {"error": f"no Yahoo mapping for {exchange}:{symbol}",
                        "symbol": symbol, "exchange": exchange, "timeframe": timeframe}
            source = "yahoo"
            interval, rng = yh_interval
            rows = _fetch_yahoo_ohlcv(yahoo_sym, interval, rng)
            if timeframe == "4h" and interval == "60m":
                rows = _resample_to_4h(rows)
    except Exception as e:
        _log.warning("fallback fetch failed for %s:%s: %s", exchange, symbol, e)
        return {"error": f"fallback fetch failed: {e}",
                "symbol": symbol, "exchange": exchange, "timeframe": timeframe}

    if len(rows) < 30:
        return {"error": f"not enough candles for indicators (got {len(rows)}, need ≥30)",
                "symbol": symbol, "exchange": exchange, "timeframe": timeframe}

    indicators = _build_tv_shaped_indicators(rows)
    metrics = compute_metrics(indicators)
    if not metrics:
        return {"error": "fallback could not compute metrics (insufficient indicator data)",
                "symbol": symbol, "exchange": exchange, "timeframe": timeframe}

    extended = extract_extended_indicators(indicators)
    tf_context = analyze_timeframe_context(indicators, timeframe)

    full_symbol = (yahoo_sym if source == "yahoo" else _stooq_symbol(symbol).upper()) or symbol.upper()

    trade_data: dict = {}
    if is_stock_exchange(exchange):
        score_result = compute_stock_score(indicators)
        if score_result:
            trade_data["stock_score"] = score_result["score"]
            trade_data["grade"] = score_result["grade"]
            trade_data["trend_state"] = score_result["trend_state"]
            setup = compute_trade_setup(indicators)
            if setup:
                trade_data["trade_setup"] = {
                    "setup_types": setup["setup_types"],
                    "entry_points": setup["entry_points"],
                    "stop_loss": setup["stop_loss"],
                    "stop_distance_pct": setup["stop_distance_pct"],
                    "targets": setup["targets"],
                    "risk_reward": setup["risk_reward"],
                    "supports": setup["supports"],
                    "resistances": setup["resistances"],
                }
                quality = compute_trade_quality(indicators, score_result["score"], setup)
                if quality:
                    trade_data["trade_quality_score"] = quality["trade_quality_score"]
                    trade_data["trade_quality"] = quality["quality"]
                    trade_data["trade_notes"] = quality["notes"]

    return {
        "symbol": full_symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data_source": "yahoo_fallback",
        "fallback_source": source,
        "candles_used": len(rows),
        "price_data": {
            "current_price": metrics["price"],
            "open":  round(indicators["open"], 6),
            "high":  round(indicators["high"], 6),
            "low":   round(indicators["low"], 6),
            "close": round(indicators["close"], 6),
            "change_percent": metrics["change"],
            "volume": indicators["volume"],
        },
        "timeframe_context": tf_context,
        "rsi": extended["rsi"],
        "macd": extended["macd"],
        "sma": extended["sma"],
        "ema": extended["ema"],
        "bollinger_bands": extended["bollinger_bands"],
        "atr": extended["atr"],
        "volume_analysis": extended["volume"],
        "obv": extended["obv"],
        "support_resistance": extended["support_resistance"],
        "stochastic": extended["stochastic"],
        "adx": extended["adx"],
        "market_structure": extended["market_structure"],
        "market_sentiment": {
            "overall_rating": metrics["rating"],
            "buy_sell_signal": metrics["signal"],
            "volatility": (
                "High" if metrics["bbw"] and metrics["bbw"] > 0.05
                else "Medium" if metrics["bbw"] and metrics["bbw"] > 0.02
                else "Low"
            ),
            "momentum": "Bullish" if metrics["change"] > 0 else "Bearish",
            "note": "Computed locally from OHLC — TradingView's proprietary rating not available.",
        },
        **trade_data,
    }
