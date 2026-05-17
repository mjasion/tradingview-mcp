"""Tests for the tv_scanner wrapper — retries, outage classification, cache."""
from __future__ import annotations

import json

import pytest

from tradingview_mcp.core.services import tv_scanner as tv


@pytest.fixture(autouse=True)
def _isolated_cache():
    tv.reset_cache_for_tests()
    yield
    tv.reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Keep retries instantaneous — the backoff math is verified separately."""
    monkeypatch.setattr(tv.time, "sleep", lambda _s: None)
    yield


class _FakeAnalysis:
    def __init__(self, indicators):
        self.indicators = indicators


def _ok_payload(symbol="NASDAQ:AAPL"):
    return {symbol: _FakeAnalysis({"close": 100.0, "open": 99.0, "RSI": 55})}


# ── Happy path + cache ────────────────────────────────────────────────────────


def test_ta_call_returns_payload_on_first_try(monkeypatch):
    calls = []

    def fake(*, screener, interval, symbols):
        calls.append((screener, interval, tuple(symbols)))
        return _ok_payload()

    monkeypatch.setattr(tv, "get_multiple_analysis", fake)
    out = tv.ta_call("america", "1D", ["NASDAQ:AAPL"])
    assert "NASDAQ:AAPL" in out
    assert len(calls) == 1


def test_ta_call_caches_repeated_calls(monkeypatch):
    calls = []

    def fake(*, screener, interval, symbols):
        calls.append(1)
        return _ok_payload()

    monkeypatch.setattr(tv, "get_multiple_analysis", fake)
    tv.ta_call("america", "1D", ["NASDAQ:AAPL"])
    tv.ta_call("america", "1D", ["NASDAQ:AAPL"])
    assert len(calls) == 1  # second call served from cache


def test_ta_call_cache_keyed_by_symbols_set(monkeypatch):
    """Order doesn't matter — same set hits same cache entry."""
    calls = []

    def fake(*, screener, interval, symbols):
        calls.append(tuple(symbols))
        return {s: _FakeAnalysis({"close": 1}) for s in symbols}

    monkeypatch.setattr(tv, "get_multiple_analysis", fake)
    tv.ta_call("america", "1D", ["NASDAQ:AAPL", "NASDAQ:MSFT"])
    tv.ta_call("america", "1D", ["NASDAQ:MSFT", "NASDAQ:AAPL"])  # reversed
    assert len(calls) == 1


def test_use_cache_false_bypasses_cache(monkeypatch):
    calls = []

    def fake(*, screener, interval, symbols):
        calls.append(1)
        return _ok_payload()

    monkeypatch.setattr(tv, "get_multiple_analysis", fake)
    tv.ta_call("america", "1D", ["NASDAQ:AAPL"])
    tv.ta_call("america", "1D", ["NASDAQ:AAPL"], use_cache=False)
    assert len(calls) == 2


# ── Outage detection + retries ────────────────────────────────────────────────


def test_json_decode_error_triggers_retries_then_raises_unavailable(monkeypatch):
    calls = []

    def flaky(*, screener, interval, symbols):
        calls.append(1)
        raise json.JSONDecodeError("Expecting value", "doc", 0)

    monkeypatch.setattr(tv, "get_multiple_analysis", flaky)
    with pytest.raises(tv.TVScannerUnavailable):
        tv.ta_call("crypto", "15m", ["BINANCE:BTCUSDT"])
    # 1 initial + len(_RETRY_DELAYS) retries
    assert len(calls) == 1 + len(tv._RETRY_DELAYS)


def test_retry_recovers_when_upstream_comes_back(monkeypatch):
    calls = []

    def flaky_then_ok(*, screener, interval, symbols):
        calls.append(1)
        if len(calls) < 2:
            raise json.JSONDecodeError("Expecting value", "doc", 0)
        return _ok_payload("BINANCE:BTCUSDT")

    monkeypatch.setattr(tv, "get_multiple_analysis", flaky_then_ok)
    out = tv.ta_call("crypto", "15m", ["BINANCE:BTCUSDT"])
    assert "BINANCE:BTCUSDT" in out
    assert len(calls) == 2


def test_connection_error_treated_as_outage(monkeypatch):
    def boom(*, screener, interval, symbols):
        raise ConnectionError("scanner refused connection")

    monkeypatch.setattr(tv, "get_multiple_analysis", boom)
    with pytest.raises(tv.TVScannerUnavailable):
        tv.ta_call("crypto", "1h", ["BINANCE:ETHUSDT"])


def test_typeerror_in_library_is_not_retried(monkeypatch):
    """Programming errors (wrong arg type) should propagate immediately."""
    calls = []

    def bad(*, screener, interval, symbols):
        calls.append(1)
        raise TypeError("interval must be a str")

    monkeypatch.setattr(tv, "get_multiple_analysis", bad)
    with pytest.raises(TypeError):
        tv.ta_call("crypto", "15m", ["X"])
    assert len(calls) == 1  # no retry


# ── Empty result classification ───────────────────────────────────────────────


def test_empty_dict_raises_scanner_empty(monkeypatch):
    monkeypatch.setattr(tv, "get_multiple_analysis", lambda **_: {})
    with pytest.raises(tv.TVScannerEmpty):
        tv.ta_call("america", "1D", ["NASDAQ:NOPE"])


def test_none_result_is_treated_as_outage(monkeypatch):
    """tradingview_ta sometimes returns None — should be classified as outage,
    not as a permanent ticker miss."""
    monkeypatch.setattr(tv, "get_multiple_analysis", lambda **_: None)
    with pytest.raises(tv.TVScannerUnavailable):
        tv.ta_call("crypto", "1D", ["BINANCE:BTCUSDT"])


def test_empty_symbol_list_raises_immediately(monkeypatch):
    monkeypatch.setattr(tv, "get_multiple_analysis", lambda **_: pytest.fail("should not be called"))
    with pytest.raises(tv.TVScannerEmpty):
        tv.ta_call("crypto", "1D", [])


# ── ta_call_or_error ──────────────────────────────────────────────────────────


def test_ta_call_or_error_success(monkeypatch):
    monkeypatch.setattr(tv, "get_multiple_analysis", lambda **_: _ok_payload())
    res = tv.ta_call_or_error("america", "1D", ["NASDAQ:AAPL"])
    assert res["_ok"] is True
    assert "NASDAQ:AAPL" in res["analysis"]


def test_ta_call_or_error_outage(monkeypatch):
    def boom(**_):
        raise json.JSONDecodeError("Expecting value", "doc", 0)

    monkeypatch.setattr(tv, "get_multiple_analysis", boom)
    res = tv.ta_call_or_error("crypto", "15m", ["BINANCE:BTCUSDT"])
    assert res["_ok"] is False
    assert res["upstream_status"] == "down"
    assert res["error"] == "tradingview_scanner_unavailable"
    assert "retry_hint" in res


def test_ta_call_or_error_empty(monkeypatch):
    monkeypatch.setattr(tv, "get_multiple_analysis", lambda **_: {})
    res = tv.ta_call_or_error("america", "1D", ["NASDAQ:NOPE"])
    assert res["_ok"] is False
    assert res["upstream_status"] == "empty"
    assert res["error"] == "no_data"
