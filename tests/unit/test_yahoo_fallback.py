"""Tests for the Yahoo fallback path used when TradingView scanner is down.

These tests focus on:
* Symbol mapping per exchange (crypto BTCUSDT → BTC-USD, XETRA:SAP → SAP.DE)
* The locally-computed indicator panel matches the analyze_coin output shape
* Insufficient-data and bad-mapping branches return a clean error dict
"""
from __future__ import annotations

import pytest

from tradingview_mcp.core.services import yahoo_fallback as yf


def _synth_candles(n: int, start: float = 100.0, vol: float = 1.0) -> list[dict]:
    """Deterministic ramp + light oscillation — enough for all indicators."""
    rows = []
    for i in range(n):
        # Mild uptrend with a sinusoidal wiggle so RSI/MACD aren't degenerate.
        import math
        drift = i * 0.05
        wiggle = math.sin(i / 3.5) * vol
        close = start + drift + wiggle
        open_ = close - 0.3
        high = close + 0.4
        low = close - 0.5
        rows.append({"ts": 1700000000 + i * 86400,
                     "open": open_, "high": high, "low": low, "close": close,
                     "volume": 10_000 + i * 50})
    return rows


# ── Symbol mapping ────────────────────────────────────────────────────────────


def test_crypto_usdt_maps_to_dash_usd():
    assert yf._yahoo_symbol("BTCUSDT", "BINANCE") == "BTC-USD"
    assert yf._yahoo_symbol("ETHUSDT", "kucoin") == "ETH-USD"


def test_xetra_appends_de_suffix():
    assert yf._yahoo_symbol("SAP", "XETRA") == "SAP.DE"
    assert yf._yahoo_symbol("SAP", "xetr") == "SAP.DE"


def test_nasdaq_uses_bare_symbol():
    assert yf._yahoo_symbol("AAPL", "NASDAQ") == "AAPL"


def test_gpw_returns_none_to_route_through_stooq():
    assert yf._yahoo_symbol("KGH", "GPW") is None
    assert yf._yahoo_symbol("CDR", "wse") is None


# ── Indicator panel from local OHLC ───────────────────────────────────────────


def test_indicator_panel_populates_tradingview_keys():
    rows = _synth_candles(60)
    ind = yf._build_tv_shaped_indicators(rows)
    # Core fields needed by extract_extended_indicators:
    for key in ("close", "open", "high", "low", "volume",
                "SMA20", "SMA50", "EMA20", "EMA50",
                "RSI", "MACD.macd", "MACD.signal",
                "BB.upper", "BB.lower", "ATR", "volume.SMA20"):
        assert ind.get(key) is not None, f"missing {key}"


def test_pivot_points_computed_from_previous_candle():
    rows = _synth_candles(30)
    ind = yf._build_tv_shaped_indicators(rows)
    prev = rows[-2]
    expected_pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
    assert ind["Pivot.M.Classic.Middle"] == pytest.approx(expected_pivot)


def test_stoch_k_within_0_100_band():
    rows = _synth_candles(40)
    ind = yf._build_tv_shaped_indicators(rows)
    assert 0 <= ind["Stoch.K"] <= 100


# ── analyze_coin_yahoo_fallback wiring ────────────────────────────────────────


def _patch_fetch(monkeypatch, candles, *, source: str = "yahoo"):
    if source == "yahoo":
        monkeypatch.setattr(yf, "_fetch_yahoo_ohlcv",
                            lambda symbol, interval, range_: candles)
    else:
        monkeypatch.setattr(yf, "_fetch_stooq_ohlcv",
                            lambda symbol, interval: candles)


def test_fallback_returns_analyze_coin_shape(monkeypatch):
    _patch_fetch(monkeypatch, _synth_candles(60))
    out = yf.analyze_coin_yahoo_fallback("AAPL", "NASDAQ", "1D")
    assert "error" not in out
    assert out["data_source"] == "yahoo_fallback"
    assert out["fallback_source"] == "yahoo"
    # Keys expected by callers of analyze_coin:
    for key in ("price_data", "rsi", "macd", "sma", "ema",
                "bollinger_bands", "atr", "volume_analysis",
                "market_sentiment", "market_structure"):
        assert key in out, f"missing {key} in fallback output"
    assert out["rsi"]["value"] is not None
    assert isinstance(out["price_data"]["current_price"], (int, float))


def test_fallback_uses_stooq_for_gpw(monkeypatch):
    _patch_fetch(monkeypatch, _synth_candles(60), source="stooq")
    out = yf.analyze_coin_yahoo_fallback("KGH", "GPW", "1D")
    assert out.get("fallback_source") == "stooq"
    assert out["data_source"] == "yahoo_fallback"


def test_insufficient_candles_returns_error(monkeypatch):
    _patch_fetch(monkeypatch, _synth_candles(15))
    out = yf.analyze_coin_yahoo_fallback("AAPL", "NASDAQ", "1D")
    assert "error" in out
    assert "not enough candles" in out["error"]


def test_unmapped_exchange_returns_error(monkeypatch):
    """Crypto symbol without USDT suffix on a crypto exchange has no mapping."""
    monkeypatch.setattr(yf, "_fetch_yahoo_ohlcv",
                        lambda *a, **kw: pytest.fail("should not be called"))
    out = yf.analyze_coin_yahoo_fallback("BTC", "BINANCE", "1D")
    assert "error" in out
    assert "no Yahoo mapping" in out["error"]


def test_fetch_failure_is_caught_and_reported(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(yf, "_fetch_yahoo_ohlcv", boom)
    out = yf.analyze_coin_yahoo_fallback("AAPL", "NASDAQ", "1D")
    assert "error" in out
    assert "fallback fetch failed" in out["error"]


# ── 4h resampling ─────────────────────────────────────────────────────────────


def test_4h_resampling_merges_four_hourly_into_one_bucket():
    # 1700006400 = 2023-11-15 00:00:00 UTC — aligned to a 4h bucket boundary
    # so 8 hourly candles map cleanly to exactly 2 four-hour buckets.
    start_ts = 1700006400
    hourly = []
    for i in range(8):  # 8 hours = 2 four-hour buckets
        hourly.append({"ts": start_ts + i * 3600,
                       "open": 100 + i, "high": 100 + i + 1,
                       "low": 100 + i - 1, "close": 100 + i + 0.5,
                       "volume": 1000})
    out = yf._resample_to_4h(hourly)
    assert len(out) == 2
    # Each bucket should aggregate 4 hours of volume
    assert out[0]["volume"] == 4000
    assert out[1]["volume"] == 4000
    # OHLC: open from first hourly in bucket, close from last
    assert out[0]["open"] == 100
