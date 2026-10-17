"""TradingView 429 handling — status-aware fetch, shared backoff gate, circuit
breaker, stale cache, and what the tools report.

The bug this pins: CloudFront answered ``429`` with an empty body,
tradingview_ta fed that to ``json.loads``, and the wrapper retried it as a
"blip" four times in four seconds — every call deepened the block and the
tools reported ``tv_down`` / ``No data found`` instead of "rate-limited".
"""
from __future__ import annotations

import json

import pytest

from tradingview_mcp.core.services import tv_scanner as tv
from tradingview_ta.main import TradingView


class _Clock:
    """Fake monotonic clock; ``sleep`` advances it instead of blocking."""

    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(tv, "_now", c.now)
    monkeypatch.setattr(tv.time, "sleep", c.sleep)
    monkeypatch.setattr(tv, "_429_BACKOFF", 5.0)
    monkeypatch.setattr(tv, "_429_MAX_BACKOFF", 60.0)
    monkeypatch.setattr(tv, "_429_BUDGET", 60.0)
    monkeypatch.setattr(tv, "_BREAKER_TTL", 300.0)
    tv.reset_cache_for_tests()
    yield c
    tv.reset_cache_for_tests()


class _Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


def _row(symbol="NASDAQ:AAPL", value=50.0):
    return {"s": symbol, "d": [value] * len(TradingView.indicators)}


def _ok_text(*symbols):
    return json.dumps({"totalCount": len(symbols), "data": [_row(s) for s in symbols]})


def _limited():
    return tv.TVScannerRateLimited("429")


class _FakeAnalysis:
    def __init__(self, indicators):
        self.indicators = indicators


# ── Status-aware fetch ────────────────────────────────────────────────────────


def test_fetch_parses_200_like_the_library(monkeypatch):
    monkeypatch.setattr(tv.requests, "post", lambda *a, **k: _Resp(200, _ok_text("NASDAQ:AAPL")))
    out = tv.get_multiple_analysis(screener="america", interval="1d",
                                   symbols=["NASDAQ:AAPL", "NASDAQ:NOPE"])
    assert out["NASDAQ:AAPL"].indicators["RSI"] == 50.0
    assert out["NASDAQ:AAPL"].exchange == "NASDAQ"
    assert out["NASDAQ:NOPE"] is None  # misses still map to None


def test_fetch_classifies_empty_429_as_rate_limited(monkeypatch):
    monkeypatch.setattr(tv.requests, "post", lambda *a, **k: _Resp(429, ""))
    with pytest.raises(tv.TVScannerRateLimited) as ei:
        tv.get_multiple_analysis(screener="america", interval="1d", symbols=["NASDAQ:AAPL"])
    assert ei.value.reason == "http_429"
    assert isinstance(ei.value, tv.TVScannerUnavailable)  # old handlers still catch it


def test_fetch_honours_retry_after(monkeypatch):
    monkeypatch.setattr(tv.requests, "post", lambda *a, **k: _Resp(429, "", {"Retry-After": "42"}))
    with pytest.raises(tv.TVScannerRateLimited) as ei:
        tv.get_multiple_analysis(screener="america", interval="1d", symbols=["NASDAQ:AAPL"])
    assert ei.value.retry_after == 42.0


def test_fetch_5xx_is_outage_and_400_is_a_bug(monkeypatch):
    monkeypatch.setattr(tv.requests, "post", lambda *a, **k: _Resp(503, ""))
    with pytest.raises(tv.TVScannerUnavailable):
        tv.get_multiple_analysis(screener="america", interval="1d", symbols=["NASDAQ:AAPL"])
    monkeypatch.setattr(tv.requests, "post", lambda *a, **k: _Resp(400, '{"error":"Unknown field"}'))
    with pytest.raises(RuntimeError, match="http_400"):
        tv.get_multiple_analysis(screener="america", interval="1d", symbols=["NASDAQ:AAPL"])


# ── Backoff gate ──────────────────────────────────────────────────────────────


def test_429_waits_with_growing_backoff_then_succeeds(monkeypatch, clock):
    calls = []

    def fake(*, screener, interval, symbols):
        calls.append(clock.t)
        if len(calls) <= 2:
            raise _limited()
        return {"NASDAQ:AAPL": _FakeAnalysis({"close": 1})}

    monkeypatch.setattr(tv, "get_multiple_analysis", fake)
    out = tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    assert "NASDAQ:AAPL" in out
    assert clock.slept == [5.0, 10.0]          # doubled, not the 0.4s blip ladder
    assert tv.rate_limit_state() is None       # success closed the gate


def test_429_storm_gives_up_at_budget_and_trips_breaker(monkeypatch, clock):
    calls = []

    def fake(**_):
        calls.append(clock.t)
        raise _limited()

    monkeypatch.setattr(tv, "get_multiple_analysis", fake)
    with pytest.raises(tv.TVScannerRateLimited) as ei:
        tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    # 0 → 5 → 15 → 35; the next wait (40s) would end past the 60s budget.
    assert len(calls) == 4
    assert ei.value.reason == "http_429"
    state = tv.rate_limit_state()
    assert state["state"] == "open"
    assert state["retry_after_seconds"] >= 299

    # Breaker open: the next call doesn't touch TradingView or wait.
    calls.clear()
    slept_before = len(clock.slept)
    with pytest.raises(tv.TVScannerRateLimited) as ei:
        tv.ta_call("america", "1d", ["NASDAQ:MSFT"])
    assert ei.value.reason == "circuit_open"
    assert calls == []
    assert len(clock.slept) == slept_before


def test_breaker_probes_after_ttl_and_closes_on_success(monkeypatch, clock):
    monkeypatch.setattr(tv, "get_multiple_analysis", lambda **_: (_ for _ in ()).throw(_limited()))
    with pytest.raises(tv.TVScannerRateLimited):
        tv.ta_call("america", "1d", ["NASDAQ:AAPL"])

    clock.t += 301
    monkeypatch.setattr(tv, "get_multiple_analysis",
                        lambda **_: {"NASDAQ:AAPL": _FakeAnalysis({"close": 1})})
    assert "NASDAQ:AAPL" in tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    assert tv.rate_limit_state() is None


def test_breaker_ttl_zero_disables_the_hold(monkeypatch, clock):
    monkeypatch.setattr(tv, "_BREAKER_TTL", 0.0)
    monkeypatch.setattr(tv, "get_multiple_analysis", lambda **_: (_ for _ in ()).throw(_limited()))
    with pytest.raises(tv.TVScannerRateLimited):
        tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    assert tv.rate_limit_state()["state"] == "backing_off"
    assert tv.rate_limit_state()["retry_after_seconds"] <= 60


def test_gate_is_shared_so_other_calls_wait_instead_of_asking(monkeypatch, clock):
    """A 429 seen by one call holds back the next one; it must not fire into
    the open 429 straight away."""
    calls = []

    def fake(*, screener, interval, symbols):
        calls.append((clock.t, symbols[0]))
        if len(calls) == 1:
            raise _limited()
        return {symbols[0]: _FakeAnalysis({"close": 1})}

    monkeypatch.setattr(tv, "get_multiple_analysis", fake)
    tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    tv._rl_note_429(None)  # another worker just got a 429
    t0 = clock.t
    tv.ta_call("america", "1d", ["NASDAQ:MSFT"])
    assert calls[-1][0] >= t0 + 5.0


# ── Stale cache ───────────────────────────────────────────────────────────────


def _prime(monkeypatch, interval="1d"):
    monkeypatch.setattr(tv, "get_multiple_analysis",
                        lambda **_: {"NASDAQ:AAPL": _FakeAnalysis({"close": 1})})
    tv.ta_call("america", interval, ["NASDAQ:AAPL"])
    # Age the entry past the 90s fresh window.
    key = next(iter(tv._CACHE))
    ts, val = tv._CACHE[key]
    tv._CACHE[key] = (ts - 600, val)
    monkeypatch.setattr(tv, "get_multiple_analysis", lambda **_: (_ for _ in ()).throw(_limited()))


def test_stale_served_only_when_asked(monkeypatch):
    _prime(monkeypatch)
    with pytest.raises(tv.TVScannerRateLimited):
        tv.ta_call("america", "1d", ["NASDAQ:AAPL"])

    tv.reset_rate_limit_for_tests()
    out = tv.ta_call("america", "1d", ["NASDAQ:AAPL"], allow_stale=True)
    assert isinstance(out, tv.StaleAnalysis)
    assert out["NASDAQ:AAPL"].indicators == {"close": 1}
    assert out.age_seconds >= 600
    fields = tv.stale_fields(out)
    assert fields["degraded"] is True
    assert fields["data_source"] == "tradingview_cache"
    assert fields["upstream_status"] == "rate_limited"
    assert fields["cache_age_seconds"] >= 600


def test_stale_capped_at_four_bars(monkeypatch):
    """A 15m request must not be answered with a reading older than an hour."""
    _prime(monkeypatch, interval="15m")
    key = next(iter(tv._CACHE))
    ts, val = tv._CACHE[key]
    tv._CACHE[key] = (ts - 3600, val)  # now ~70 min old
    with pytest.raises(tv.TVScannerRateLimited):
        tv.ta_call("america", "15m", ["NASDAQ:AAPL"], allow_stale=True)


def test_fresh_result_has_no_stale_fields(monkeypatch):
    monkeypatch.setattr(tv, "get_multiple_analysis",
                        lambda **_: {"NASDAQ:AAPL": _FakeAnalysis({"close": 1})})
    assert tv.stale_fields(tv.ta_call("america", "1d", ["NASDAQ:AAPL"])) == {}


# ── What callers report ───────────────────────────────────────────────────────


def test_upstream_fields_distinguish_429_from_outage():
    rl = tv.upstream_fields(tv.TVScannerRateLimited("x", reason="circuit_open", retry_after=12.3))
    assert rl == {"upstream_status": "rate_limited", "upstream_reason": "circuit_open",
                  "retry_after_seconds": 12}
    assert tv.upstream_fields(tv.TVScannerUnavailable("x")) == {"upstream_status": "down"}


def test_ta_call_or_error_reports_rate_limited(monkeypatch):
    monkeypatch.setattr(tv, "get_multiple_analysis", lambda **_: (_ for _ in ()).throw(_limited()))
    res = tv.ta_call_or_error("america", "1d", ["NASDAQ:AAPL"])
    assert res["_ok"] is False
    assert res["upstream_status"] == "rate_limited"


def test_coin_analysis_reports_cached_reading_not_real_time(monkeypatch):
    from tradingview_mcp.core.services import screener_service as ss

    monkeypatch.setattr(tv.requests, "post", lambda *a, **k: _Resp(200, _ok_text("NASDAQ:AAPL")))
    fresh = ss.analyze_coin("AAPL", "nasdaq", "1d")
    assert fresh["timestamp"] == "real-time"
    assert "degraded" not in fresh

    key = next(iter(tv._CACHE))
    ts, val = tv._CACHE[key]
    tv._CACHE[key] = (ts - 600, val)
    monkeypatch.setattr(tv.requests, "post", lambda *a, **k: _Resp(429, ""))
    stale = ss.analyze_coin("AAPL", "nasdaq", "1d")
    assert stale["timestamp"] == "cached"
    assert stale["degraded"] is True
    assert stale["data_source"] == "tradingview_cache"
    assert stale["upstream_status"] == "rate_limited"
    assert stale["price_data"] == fresh["price_data"]


def test_coin_analysis_fallback_says_rate_limited(monkeypatch):
    from tradingview_mcp.core.services import screener_service as ss
    from tradingview_mcp.core.services import yahoo_fallback as yf

    monkeypatch.setattr(tv.requests, "post", lambda *a, **k: _Resp(429, ""))
    monkeypatch.setattr(yf, "analyze_coin_yahoo_fallback",
                        lambda *a, **k: {"symbol": "AAPL", "data_source": "yahoo_fallback"})
    out = ss.analyze_coin("AAPL", "nasdaq", "1d")
    assert out["data_source"] == "yahoo_fallback"
    assert out["upstream_status"] == "rate_limited"
    assert out["degraded"] is True
