"""Tests for portfolio_scan — the watchlist orchestrator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradingview_mcp.core.services import cache as cache_mod
from tradingview_mcp.core.services import portfolio_service as ps


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_DIR", str(tmp_path))
    cache_mod.reset_for_tests()
    yield
    cache_mod.reset_for_tests()


def _ta(rsi=50, bb="Upper Half", vol="Medium"):
    return {
        "price_data": {"current_price": 100.0, "change_percent": 1.2},
        "rsi": {"value": rsi},
        "bollinger_bands": {"position": bb},
        "atr": {"volatility": vol},
        "market_sentiment": {"overall_rating": "Buy"},
    }


def _patch_services(monkeypatch, *, ta_map=None, earnings_map=None,
                    dividends_map=None, news_count=0):
    ta_map = ta_map or {}
    earnings_map = earnings_map or {}
    dividends_map = dividends_map or {}

    monkeypatch.setattr(ps, "analyze_coin",
                        lambda symbol, exchange, timeframe: ta_map.get(symbol, _ta()))
    monkeypatch.setattr(ps, "get_earnings",
                        lambda symbol: earnings_map.get(symbol, {"days_until": None}))
    monkeypatch.setattr(ps, "get_dividends",
                        lambda symbol: dividends_map.get(symbol, {"next_ex_date": None}))
    monkeypatch.setattr(ps, "fetch_news_summary",
                        lambda symbol=None, category="stocks", limit=10:
                        {"count": news_count, "items": []})
    monkeypatch.setattr(ps, "get_insider_transactions",
                        lambda symbol, limit=5: {"count": 0, "filings": []})


def test_empty_symbols_returns_empty_results():
    out = ps.portfolio_scan([])
    assert out["results"] == []
    assert out["summary"]["scanned"] == 0


def test_rsi_overbought_flag(monkeypatch):
    _patch_services(monkeypatch, ta_map={"AAPL": _ta(rsi=78)})
    out = ps.portfolio_scan(["AAPL"])
    flags = out["results"][0]["flags"]
    assert any(f.startswith("rsi_overbought") for f in flags)
    assert "rsi_oversold" not in " ".join(flags)


def test_rsi_oversold_and_bb_below_lower(monkeypatch):
    _patch_services(monkeypatch, ta_map={"X": _ta(rsi=22, bb="Below Lower Band")})
    out = ps.portfolio_scan(["X"])
    flags = out["results"][0]["flags"]
    assert any(f.startswith("rsi_oversold") for f in flags)
    assert "bb_below_lower" in flags


def test_earnings_within_horizon(monkeypatch):
    _patch_services(monkeypatch, earnings_map={"AAPL": {"days_until": 3, "next_earnings_date": "2026-05-06"}})
    out = ps.portfolio_scan(["AAPL"])
    assert "earnings_in_3d" in out["results"][0]["flags"]


def test_earnings_outside_horizon_no_flag(monkeypatch):
    _patch_services(monkeypatch, earnings_map={"AAPL": {"days_until": 30}})
    out = ps.portfolio_scan(["AAPL"])
    assert not any("earnings_in" in f for f in out["results"][0]["flags"])


def test_ex_dividend_within_horizon(monkeypatch):
    soon = (datetime.now(timezone.utc).date() + timedelta(days=4)).isoformat()
    _patch_services(monkeypatch, dividends_map={"KO": {"next_ex_date": soon}})
    out = ps.portfolio_scan(["KO"])
    assert any(f.startswith("ex_dividend_in_") for f in out["results"][0]["flags"])


def test_news_active_threshold(monkeypatch):
    _patch_services(monkeypatch, news_count=8)
    out = ps.portfolio_scan(["AAPL"])
    assert any(f.startswith("news_active") for f in out["results"][0]["flags"])


def test_results_preserve_input_order(monkeypatch):
    _patch_services(monkeypatch)
    out = ps.portfolio_scan(["AAA", "BBB", "CCC", "DDD"])
    assert [r["symbol"] for r in out["results"]] == ["AAA", "BBB", "CCC", "DDD"]


def test_ta_error_does_not_block_scan(monkeypatch):
    def fake_ta(symbol, exchange, timeframe):
        if symbol == "BAD":
            return {"error": "unknown symbol"}
        return _ta()

    monkeypatch.setattr(ps, "analyze_coin", fake_ta)
    monkeypatch.setattr(ps, "get_earnings", lambda s: {"days_until": None})
    monkeypatch.setattr(ps, "get_dividends", lambda s: {"next_ex_date": None})
    monkeypatch.setattr(ps, "fetch_news_summary",
                        lambda symbol=None, category="stocks", limit=10:
                        {"count": 0, "items": []})
    monkeypatch.setattr(ps, "get_insider_transactions",
                        lambda symbol, limit=5: {"count": 0, "filings": []})

    out = ps.portfolio_scan(["BAD", "AAPL"])
    by_sym = {r["symbol"]: r for r in out["results"]}
    assert "ta_error" in by_sym["BAD"]
    assert "ta_error" not in by_sym["AAPL"]
    assert out["summary"]["scanned"] == 2
    assert out["summary"]["errors"] == 1


def test_include_insider_attaches_form4_count(monkeypatch):
    _patch_services(monkeypatch)
    monkeypatch.setattr(ps, "get_insider_transactions",
                        lambda symbol, limit=5: {"count": 7, "filings": [
                            {"date": "2026-04-01", "accession": "x", "url": "u"}
                        ]})
    out = ps.portfolio_scan(["AAPL"], include_insider=True)
    r = out["results"][0]
    assert r["insider_form4_count"] == 7
