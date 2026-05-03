"""Tests for Reddit sentiment dedup by permalink across subreddits."""
from __future__ import annotations

from tradingview_mcp.core.services import sentiment_service


def _post(permalink: str, title: str = "AAPL bullish strong buy", score: int = 100) -> dict:
    return {"data": {
        "permalink": permalink,
        "id": permalink.split("/")[-1] or "id",
        "title": title,
        "selftext": "",
        "score": score,
        "num_comments": 5,
    }}


def test_sentiment_dedups_crosspost_across_subreddits(monkeypatch):
    """A cross-post visible in multiple subreddits must count exactly once."""
    duplicate = _post("/r/wsb/comments/abc123/duplicate/")
    unique_a = _post("/r/stocks/comments/aaa111/uniq_a/")
    unique_b = _post("/r/investing/comments/bbb222/uniq_b/")

    feeds = {
        "wallstreetbets": [duplicate, unique_a],
        "stocks":         [duplicate, unique_b],   # same permalink as wsb
        "investing":      [duplicate],             # again the same permalink
        "CryptoCurrency": [],
        "StockMarket":    [],
    }

    monkeypatch.setattr(
        sentiment_service,
        "_fetch_reddit_posts",
        lambda sub, query, limit: feeds.get(sub, []),
    )

    result = sentiment_service.analyze_sentiment("AAPL", category="all")

    # 3 unique posts (duplicate counted once, plus uniq_a, uniq_b)
    assert result["posts_analyzed"] == 3
    # Top posts list should contain at most 3 unique items
    urls = {p["url"] for p in result["top_posts"]}
    assert len(urls) == result["posts_analyzed"]


def test_sentiment_uses_id_when_permalink_missing(monkeypatch):
    """If a post has no permalink, falls back to id-based dedup."""
    p1 = {"data": {"permalink": "", "id": "x1", "title": "long", "selftext": "", "score": 1, "num_comments": 0}}
    p2 = {"data": {"permalink": "", "id": "x1", "title": "long", "selftext": "", "score": 1, "num_comments": 0}}
    monkeypatch.setattr(
        sentiment_service,
        "_fetch_reddit_posts",
        lambda sub, query, limit: [p1, p2] if sub == "stocks" else [],
    )
    result = sentiment_service.analyze_sentiment("AAPL", category="stocks")
    assert result["posts_analyzed"] == 1
