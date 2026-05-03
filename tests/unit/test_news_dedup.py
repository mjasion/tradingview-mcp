"""Tests for news_service dedup + freshness sort + max-age filter."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradingview_mcp.core.services import news_service


def _iso(days_ago: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat(timespec="seconds")


def test_parse_published_iso_and_rfc2822():
    iso = news_service._parse_published("2026-05-03T15:20:00+02:00")
    assert iso is not None and iso.tzinfo is not None
    rfc = news_service._parse_published("Sat, 02 May 2026 09:00:00 GMT")
    assert rfc is not None and rfc.tzinfo is not None


def test_parse_published_returns_none_for_empty_or_garbage():
    assert news_service._parse_published("") is None
    assert news_service._parse_published("not a date") is None


def test_is_stale_drops_items_older_than_max_age():
    assert news_service._is_stale("2020-01-01T00:00:00+00:00") is True
    assert news_service._is_stale(_iso(0.5)) is False
    # Edge: just over 7 days is stale
    assert news_service._is_stale(_iso(7.5)) is True


def test_is_stale_keeps_unparseable_dates():
    # Items without a parseable date must NOT be silently dropped — we'd lose
    # all PAP items if they ever stop emitting dates again.
    assert news_service._is_stale("") is False
    assert news_service._is_stale("garbage") is False


def test_fetch_news_dedups_and_sorts(monkeypatch):
    """End-to-end: dedup by (title, source) + sort newest-first."""
    fake_entries = [
        {"title": "Apple beats earnings",   "link": "u1", "published": _iso(1)},
        {"title": "Apple beats earnings",   "link": "u2", "published": _iso(2)},  # dup
        {"title": "Old news from 2020",     "link": "u3", "published": "2020-01-01T00:00:00+00:00"},  # stale
        {"title": "Tesla cuts prices",      "link": "u4", "published": _iso(0.1)},
        {"title": "Microsoft hits ATH",     "link": "u5", "published": _iso(3)},
    ]

    class _Feed:
        feed = {"title": "Fake Source"}
        entries = fake_entries

    monkeypatch.setattr(news_service.feedparser, "parse", lambda url: _Feed())
    monkeypatch.setattr(news_service, "RSS_FEEDS", {"stocks": [{"url": "x", "name": "Fake Source"}]})

    items = news_service.fetch_news(category="stocks", limit=10)

    titles = [i["title"] for i in items]
    # Dedup: "Apple beats earnings" appears once
    assert titles.count("Apple beats earnings") == 1
    # Stale dropped
    assert "Old news from 2020" not in titles
    # Sort: Tesla (0.1d) > Apple (1d) > Microsoft (3d)
    assert titles == ["Tesla cuts prices", "Apple beats earnings", "Microsoft hits ATH"]
