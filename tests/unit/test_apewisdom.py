"""ApeWisdom fallback: ranking lookup, paging, and honest failure."""
from __future__ import annotations

import pytest

from tradingview_mcp.core.services import apewisdom_service as aw


def _row(ticker: str, rank: int, mentions: int, prev=None) -> dict:
    return {"rank": rank, "ticker": ticker, "name": f"{ticker} Inc",
            "mentions": mentions, "upvotes": 10, "rank_24h_ago": rank + 1,
            "mentions_24h_ago": prev}


def _pages(monkeypatch, pages: dict[int, dict]):
    monkeypatch.setattr(aw, "_fetch_page", lambda filter_name, page: pages[page])


def test_found_on_first_page(monkeypatch):
    _pages(monkeypatch, {1: {"count": 702, "pages": 8,
                             "results": [_row("SPY", 1, 60, 128), _row("AAPL", 2, 5, 30)]}})

    r = aw.fetch_mentions("AAPL", category="stocks")

    assert r["status"] == "ok"
    assert r["rank"] == 2
    assert r["mentions_24h"] == 5
    assert r["mentions_change_pct"] == pytest.approx(-83.3)
    assert r["universe_size"] == 702
    assert "not directional sentiment" in r["measures"]
    assert "sentiment_score" not in r


def test_walks_pages_until_found(monkeypatch):
    seen: list[int] = []

    def _fetch(filter_name, page):
        seen.append(page)
        if page == 2:
            return {"count": 300, "pages": 3, "results": [_row("TEAM", 120, 3)]}
        return {"count": 300, "pages": 3, "results": [_row("SPY", 1, 60)]}

    monkeypatch.setattr(aw, "_fetch_page", _fetch)
    r = aw.fetch_mentions("TEAM", category="stocks")

    assert r["status"] == "ok" and r["rank"] == 120
    assert seen == [1, 2]  # stops as soon as it hits


def test_not_ranked_is_a_reading(monkeypatch):
    _pages(monkeypatch, {1: {"count": 200, "pages": 1, "results": [_row("SPY", 1, 60)]}})

    r = aw.fetch_mentions("TEAM", category="stocks")

    assert r["status"] == "not_ranked"
    assert r["mentions_24h"] == 0
    assert r["scanned_top"] == 1
    assert "not a data gap" in r["note"]


def test_upstream_failure_is_reported_not_swallowed(monkeypatch):
    _pages(monkeypatch, {1: {"error": "apewisdom_http_503"}})

    r = aw.fetch_mentions("AAPL", category="stocks")

    assert r["status"] == "unavailable"
    assert r["error"] == "apewisdom_http_503"
    assert "do not record it as neutral" in r["recommended_action"]


def test_stops_at_last_page(monkeypatch):
    seen: list[int] = []

    def _fetch(filter_name, page):
        seen.append(page)
        return {"count": 100, "pages": 1, "results": [_row("SPY", 1, 60)]}

    monkeypatch.setattr(aw, "_fetch_page", _fetch)
    assert aw.fetch_mentions("NVDA", max_pages=5)["status"] == "not_ranked"
    assert seen == [1]


@pytest.mark.parametrize("raw, expected", [
    ("BTCUSDT", "BTC"), ("btcusdt", "BTC"), ("BINANCE:ETHUSDT", "ETH"),
    ("AAPL", "AAPL"), ("KGH", "KGH"), ("", ""),
])
def test_symbol_normalisation(raw, expected):
    assert aw._normalize(raw) == expected


@pytest.mark.parametrize("category, expected", [
    ("stocks", "all-stocks"), ("crypto", "all-crypto"),
    ("all", "all"), ("nonsense", "all"),
])
def test_category_maps_to_filter(category, expected):
    assert aw._filter_for(category) == expected
