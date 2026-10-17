"""Fewer upstream requests for the same answers.

Every test here counts calls to a faked upstream. The point is the count:
a 10-symbol ``portfolio_scan`` used to cost ~10 TradingView, ~20 Yahoo
quoteSummary and ~60 RSS requests, and the IP-level 429s that followed were
the reason TA "didn't work".
"""
from __future__ import annotations

import json

import pytest

from tradingview_mcp.core.services import news_service
from tradingview_mcp.core.services import portfolio_service as ps
from tradingview_mcp.core.services import screener_service as ss
from tradingview_mcp.core.services import tv_scanner as tv
from tradingview_mcp.core.services import yahoo_finance_service as yfs


class _FakeAnalysis:
    def __init__(self, symbol):
        self.symbol = symbol
        self.indicators = {"close": 1.0}


def _recording_scanner(monkeypatch, known=None):
    """Fake ``get_multiple_analysis``; returns the list of symbol lists asked."""
    asked: list[tuple[str, list[str]]] = []

    def fake(*, screener, interval, symbols):
        asked.append((screener, list(symbols)))
        return {s: (_FakeAnalysis(s) if known is None or s in known else None) for s in symbols}

    monkeypatch.setattr(tv, "get_multiple_analysis", fake)
    return asked


# ── TradingView: per-symbol cache ─────────────────────────────────────────────


def test_batch_call_answers_later_single_symbol_calls(monkeypatch):
    asked = _recording_scanner(monkeypatch)
    tv.ta_call("america", "1d", ["NASDAQ:AAPL", "NASDAQ:MSFT", "NASDAQ:NVDA"])
    for s in ("NASDAQ:AAPL", "NASDAQ:MSFT", "NASDAQ:NVDA"):
        assert tv.ta_call("america", "1d", [s])[s].symbol == s
    assert len(asked) == 1


def test_only_uncached_symbols_are_requested(monkeypatch):
    asked = _recording_scanner(monkeypatch)
    tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    out = tv.ta_call("america", "1d", ["NASDAQ:AAPL", "NASDAQ:MSFT"])
    assert set(out) == {"NASDAQ:AAPL", "NASDAQ:MSFT"}
    assert asked[-1] == ("america", ["NASDAQ:MSFT"])


def test_a_miss_is_cached_too(monkeypatch):
    asked = _recording_scanner(monkeypatch, known=set())
    assert tv.ta_call("america", "1d", ["NASDAQ:NOPE"])["NASDAQ:NOPE"] is None
    assert tv.ta_call("america", "1d", ["NASDAQ:NOPE"])["NASDAQ:NOPE"] is None
    assert len(asked) == 1


def test_1D_and_1d_share_the_cache(monkeypatch):
    asked = _recording_scanner(monkeypatch)
    tv.ta_call("america", "1D", ["NASDAQ:AAPL"])
    tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    assert len(asked) == 1


# ── TradingView: outage cooldown ──────────────────────────────────────────────


@pytest.fixture
def fake_clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(tv, "_now", lambda: t[0])
    monkeypatch.setattr(tv.time, "sleep", lambda s: t.__setitem__(0, t[0] + s))
    monkeypatch.setattr(tv, "_OUTAGE_COOLDOWN", 15.0)
    return t


def test_outage_cooldown_stops_the_next_call_from_asking(monkeypatch, fake_clock):
    calls = []

    def dead(**_):
        calls.append(1)
        raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(tv, "get_multiple_analysis", dead)
    with pytest.raises(tv.TVScannerUnavailable):
        tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    burned = len(calls)
    with pytest.raises(tv.TVScannerUnavailable, match="not queried"):
        tv.ta_call("america", "1d", ["NASDAQ:MSFT"])
    assert len(calls) == burned
    # A cooldown is still an outage, not a rate limit.
    with pytest.raises(tv.TVScannerUnavailable) as ei:
        tv.ta_call("america", "1d", ["NASDAQ:MSFT"])
    assert tv.upstream_fields(ei.value) == {"upstream_status": "down"}


def test_outage_cooldown_expires(monkeypatch, fake_clock):
    monkeypatch.setattr(tv, "get_multiple_analysis",
                        lambda **_: (_ for _ in ()).throw(ConnectionError("refused")))
    with pytest.raises(tv.TVScannerUnavailable):
        tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    fake_clock[0] += 16
    asked = _recording_scanner(monkeypatch)
    tv.ta_call("america", "1d", ["NASDAQ:AAPL"])
    assert len(asked) == 1


# ── analyze_coin routing + portfolio prefetch ─────────────────────────────────


@pytest.mark.parametrize("exchange", ["NASDAQ", "nasdaq", " Nasdaq "])
def test_tv_target_matches_exchange_case_insensitively(exchange):
    """portfolio_scan passes "NASDAQ"; it used to land on the crypto screener
    and every stock came back "No data found"."""
    assert ss.tv_target("aapl", exchange) == ("NASDAQ:AAPL", "america")


def test_prefetch_is_one_request_and_analyze_coin_reuses_it(monkeypatch):
    asked = _recording_scanner(monkeypatch)
    assert ss.prefetch_ta(["AAPL", "MSFT", "NVDA"], "NASDAQ", "1D") == 3
    assert asked == [("america", ["NASDAQ:AAPL", "NASDAQ:MSFT", "NASDAQ:NVDA"])]
    monkeypatch.setattr(tv, "get_multiple_analysis",
                        lambda **_: pytest.fail("analyze_coin should read the prefetch"))
    for sym in ("AAPL", "MSFT", "NVDA"):
        assert tv.ta_call("america", "1D", [f"NASDAQ:{sym}"])


def test_prefetch_swallows_an_outage(monkeypatch, fake_clock):
    monkeypatch.setattr(tv, "get_multiple_analysis",
                        lambda **_: (_ for _ in ()).throw(ConnectionError("refused")))
    assert ss.prefetch_ta(["AAPL"], "nasdaq", "1d") == 0


def test_portfolio_scan_prefetches_the_whole_watchlist_once(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_DIR", str(tmp_path))
    prefetched = []
    monkeypatch.setattr(ps, "prefetch_ta",
                        lambda symbols, exchange, timeframe: prefetched.append(list(symbols)) or 0)
    monkeypatch.setattr(ps, "analyze_coin", lambda *a: {"error": "x"})
    monkeypatch.setattr(ps, "get_earnings", lambda s: {"error": "x"})
    monkeypatch.setattr(ps, "get_dividends", lambda s: {"error": "x"})
    monkeypatch.setattr(ps, "fetch_news_summary", lambda **k: {"count": 0, "items": []})
    ps.portfolio_scan(["aapl", "msft"], exchange="NASDAQ")
    assert prefetched == [["AAPL", "MSFT"]]


# ── Yahoo: one quoteSummary for earnings + dividends ──────────────────────────


def test_earnings_and_dividends_share_one_quote_summary(monkeypatch, tmp_path):
    from tradingview_mcp.core.services import cache as cache_mod

    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_DIR", str(tmp_path))
    cache_mod.reset_for_tests()
    calls = []

    def fake(symbol, modules):
        calls.append(tuple(modules))
        return {"calendarEvents": {"exDividendDate": {"raw": 1_900_000_000}},
                "summaryDetail": {"dividendYield": {"raw": 0.01}}}

    monkeypatch.setattr(yfs, "_fetch_quote_summary", fake)
    assert "error" not in yfs.get_earnings("SHAREDBLOCK")
    assert yfs.get_dividends("SHAREDBLOCK")["dividend_yield"] == 0.01
    assert len(calls) == 1
    assert {"calendarEvents", "earningsHistory", "summaryDetail",
            "defaultKeyStatistics"} <= set(calls[0])
    cache_mod.reset_for_tests()


def test_a_failed_quote_summary_is_not_shared(monkeypatch, tmp_path):
    from tradingview_mcp.core.services import cache as cache_mod

    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_DIR", str(tmp_path))
    cache_mod.reset_for_tests()
    calls = []

    def boom(symbol, modules):
        calls.append(1)
        raise yfs.YahooRateLimited(retry_after_seconds=5)

    monkeypatch.setattr(yfs, "_fetch_quote_summary", boom)
    yfs.get_earnings("FAILBLOCK")
    yfs.get_dividends("FAILBLOCK")
    assert len(calls) == 2
    cache_mod.reset_for_tests()


# ── News: general feeds read once per scan ────────────────────────────────────


class _Feed:
    def __init__(self, title):
        self.feed = {"title": title}
        self.entries = [{"title": "AAPL and MSFT both up", "link": "http://x",
                         "published": "", "summary": ""}]


def test_general_feeds_are_fetched_once_across_symbols(monkeypatch):
    fetched = []

    def fake(url):
        fetched.append(url)
        return _Feed(url)

    monkeypatch.setattr(news_service, "_parse_feed", fake)
    news_service.fetch_news_summary(symbol="AAPL", category="stocks", limit=5)
    first = len(fetched)
    news_service.fetch_news_summary(symbol="MSFT", category="stocks", limit=5)
    # Only MSFT's own Google News search feed is new.
    assert len(fetched) == first + 1
    assert "MSFT" in fetched[-1]


def test_a_dead_feed_is_reported_without_being_asked_again(monkeypatch):
    calls = []

    def dead(url):
        calls.append(url)
        raise news_service.urllib.error.URLError("timed out")

    monkeypatch.setattr(news_service, "_parse_feed", dead)
    r1 = news_service.fetch_news_summary(category="stocks", limit=5)
    r2 = news_service.fetch_news_summary(category="stocks", limit=5)
    assert r1["status"] == r2["status"] == "unavailable"
    assert r2["sources"]["failed"] == r1["sources"]["failed"]
    assert len(calls) == r1["sources"]["attempted"]


# ── Yahoo: crumb bootstrap is single-flight and 429-aware ─────────────────────


@pytest.fixture
def no_crumb(monkeypatch):
    monkeypatch.setitem(yfs._CRUMB_CACHE, "crumb", None)
    monkeypatch.setitem(yfs._CRUMB_CACHE, "cookies", None)
    monkeypatch.setattr(yfs.time, "sleep", lambda _s: None)
    yfs._reset_breaker()
    with yfs._RATE_LIMIT_LOCK:
        yfs._RATE_LIMIT_COOLDOWN_UNTIL = 0.0
    yield
    yfs._reset_breaker()
    with yfs._RATE_LIMIT_LOCK:
        yfs._RATE_LIMIT_COOLDOWN_UNTIL = 0.0


def _http_429():
    return yfs.urllib.error.HTTPError("https://query2.finance.yahoo.com/v1/test/getcrumb",
                                      429, "Too Many Requests", {}, None)  # type: ignore[arg-type]


def test_getcrumb_429_trips_the_breaker_instead_of_escaping(monkeypatch, no_crumb):
    """Live 2026-10-17: getcrumb answered 429, the bare HTTPError bypassed the
    breaker, and every next call bootstrapped (and 429'd) again."""
    boots = []

    def bootstrap():
        boots.append(1)
        raise _http_429()

    monkeypatch.setattr(yfs, "_bootstrap_crumb", bootstrap)
    with pytest.raises(yfs.YahooRateLimited):
        yfs._fetch_quote_summary("AAPL", ["calendarEvents"])
    assert len(boots) == 2  # one retry, then the breaker
    assert yfs._breaker_is_open()[0]

    out = yfs.get_dividends("CRUMB429")
    assert out["upstream_status"] == "rate_limited"
    assert len(boots) == 2  # breaker open: no new bootstrap


def test_parallel_workers_bootstrap_one_crumb(monkeypatch, no_crumb):
    import threading

    boots = []

    def bootstrap():
        boots.append(1)
        return "crumb", "c=1"

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"quoteSummary": {"result": [{"calendarEvents": {}}]}}'

    class _Opener:
        def open(self, req, timeout=None): return _Resp()

    monkeypatch.setattr(yfs, "_bootstrap_crumb", bootstrap)
    monkeypatch.setattr(yfs, "build_opener_with_proxy", lambda *a, **k: _Opener())
    ts = [threading.Thread(target=yfs._fetch_quote_summary, args=(s, ["calendarEvents"]))
          for s in ("AAPL", "MSFT", "NVDA")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)
    assert len(boots) == 1
