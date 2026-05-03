"""ATR / volatility classification — guard against the ATR-null → 'Low' bug."""
from __future__ import annotations

from tradingview_mcp.core.services import indicators as ind_mod


def _base_indicators() -> dict:
    """Minimal indicator dict that lets extract_extended_indicators run."""
    return {
        "close": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.5,
        "volume": 1_000_000,
        "RSI": 50.0,
        "EMA9": 100.0,
        "EMA20": 99.5,
        "EMA50": 99.0,
        "EMA200": 95.0,
        "SMA20": 99.5,
        "BB.upper": 102.0,
        "BB.lower": 97.0,
        "MACD.macd": 0.1,
        "MACD.signal": 0.05,
    }


def test_atr_null_yields_volatility_unknown():
    """When TradingView returns no ATR, volatility must be 'Unknown', not 'Low'."""
    out = ind_mod.extract_extended_indicators(_base_indicators())  # no ATR key
    assert out["atr"]["value"] is None
    assert out["atr"]["volatility"] == "Unknown"


def test_atr_present_yields_low_medium_high_buckets():
    """Sanity-check the bucketing thresholds (1.5%, 3%) used downstream."""
    base = _base_indicators()

    # Low: ATR pct ~ 0.5
    out_low = ind_mod.extract_extended_indicators({**base, "ATR": 0.5})
    assert out_low["atr"]["volatility"] == "Low"

    # Medium: ATR pct ~ 2.0
    out_med = ind_mod.extract_extended_indicators({**base, "ATR": 2.0})
    assert out_med["atr"]["volatility"] == "Medium"

    # High: ATR pct ~ 5.0
    out_hi = ind_mod.extract_extended_indicators({**base, "ATR": 5.0})
    assert out_hi["atr"]["volatility"] == "High"


def test_atr_with_zero_close_falls_back_to_unknown():
    """Division-by-zero guard — close=0 must not crash, must return 'Unknown'."""
    out = ind_mod.extract_extended_indicators({**_base_indicators(), "close": 0, "ATR": 1.0})
    assert out["atr"]["volatility"] == "Unknown"
