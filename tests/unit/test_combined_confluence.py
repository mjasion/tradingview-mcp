"""``combined_analysis`` must not score a missing sentiment pillar as agreement.

With the old code a blocked Reddit produced ``sentiment_score: 0.0``; a bearish
technical then "agreed" with that zero and the tool reported
``confidence: HIGH`` — high confidence built on data that was never fetched.
"""
from __future__ import annotations

import pytest

from tradingview_mcp import server


@pytest.fixture(autouse=True)
def _stub_upstreams(monkeypatch):
    monkeypatch.setattr(server, "coin_analysis", lambda symbol, exchange, timeframe: {
        "market_sentiment": {"momentum": "Bearish", "buy_sell_signal": "SELL"},
    })
    monkeypatch.setattr(server, "fetch_news_summary",
                        lambda symbol=None, category="stocks", limit=10: {
                            "count": 2, "items": [{"title": "a"}, {"title": "b"}],
                            "status": "ok",
                        })


def _sentiment(monkeypatch, payload):
    monkeypatch.setattr(server, "analyze_sentiment",
                        lambda symbol, category="all": payload)


def test_unavailable_sentiment_degrades_to_technical_only(monkeypatch):
    _sentiment(monkeypatch, {
        "status": "unavailable", "sentiment_score": None,
        "sentiment_label": "UNAVAILABLE", "posts_analyzed": 0,
        "reason": "every Reddit search failed (http_403)",
    })

    r = server.combined_analysis("AAPL", exchange="NASDAQ")

    assert r["confluence"]["confidence"] == "TECHNICAL_ONLY"
    assert r["confluence"]["signals_agree"] is None
    assert "unavailable" in r["confluence"]["recommendation"]


def test_mention_fallback_is_reported_as_volume_not_agreement(monkeypatch):
    _sentiment(monkeypatch, {
        "status": "degraded", "sentiment_score": None,
        "sentiment_label": "MENTIONS_ONLY", "posts_analyzed": 0,
        "mentions": {"status": "ok", "mentions_24h": 5, "rank": 37},
    })

    r = server.combined_analysis("AAPL", exchange="NASDAQ")

    assert r["confluence"]["confidence"] == "TECHNICAL_ONLY"
    assert r["confluence"]["signals_agree"] is None
    assert "5 mentions/24h" in r["confluence"]["recommendation"]


def test_no_chatter_fallback_says_so(monkeypatch):
    _sentiment(monkeypatch, {
        "status": "degraded", "sentiment_score": None,
        "sentiment_label": "MENTIONS_ONLY", "posts_analyzed": 0,
        "mentions": {"status": "not_ranked", "mentions_24h": 0},
    })

    r = server.combined_analysis("TEAM", exchange="NASDAQ")
    assert "no measurable retail chatter" in r["confluence"]["recommendation"]


def test_real_sentiment_still_scores_confluence(monkeypatch):
    """A genuine bearish reading against a bearish technical is still agreement."""
    _sentiment(monkeypatch, {
        "status": "ok", "sentiment_score": -0.4, "sentiment_label": "Strongly Bearish",
        "posts_analyzed": 12,
    })

    r = server.combined_analysis("AAPL", exchange="NASDAQ")

    assert r["confluence"]["confidence"] == "HIGH"
    assert r["confluence"]["signals_agree"] is True
    assert "12 posts analyzed" in r["confluence"]["recommendation"]


def test_gpw_path_untouched(monkeypatch):
    """GPW already skipped Reddit honestly — keep it that way."""
    called = []
    monkeypatch.setattr(server, "analyze_sentiment",
                        lambda *a, **k: called.append(1) or {})

    r = server.combined_analysis("KGH", exchange="GPW")

    assert called == []
    assert r["sentiment"]["skipped"] is True
    assert r["confluence"]["confidence"] == "TECHNICAL_ONLY"
