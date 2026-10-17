"""``count: 0`` must say which kind of zero it is.

feeds.reuters.com went NXDOMAIN and was the only source for the ``stocks``
category, so every US query returned an empty list that read as "no news".
These tests pin the distinction between a failed lookup and a real absence.
"""
from __future__ import annotations

import urllib.error

from datetime import datetime, timedelta, timezone

from tradingview_mcp.core.services import news_service


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


class _Feed:
    def __init__(self, entries, title="Fake Source"):
        self.feed = {"title": title}
        self.entries = entries


def _one_feed(monkeypatch, name="Fake Source"):
    monkeypatch.setattr(news_service, "RSS_FEEDS", {"stocks": [{"url": "x", "name": name}]})


def test_all_feeds_dead_is_unavailable_not_quiet(monkeypatch):
    _one_feed(monkeypatch)

    def _dead(url):
        raise urllib.error.URLError("nodename nor servname provided")
    monkeypatch.setattr(news_service, "_parse_feed", _dead)

    r = news_service.fetch_news_summary(category="stocks", limit=5)

    assert r["status"] == "unavailable"
    assert r["error"] == "all_feeds_failed"
    assert r["count"] == 0
    assert "Fake Source" in r["sources"]["failed"]
    assert "unreachable" in r["sources"]["failed"]["Fake Source"]
    assert "not that" in r["note"]  # spells out what the zero means


def test_http_error_is_recorded_with_its_status(monkeypatch):
    _one_feed(monkeypatch, "CNBC Finance")

    def _403(url):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    monkeypatch.setattr(news_service, "_parse_feed", _403)

    r = news_service.fetch_news_summary(category="stocks", limit=5)
    assert r["sources"]["failed"]["CNBC Finance"] == "http_403"


def test_feeds_alive_but_symbol_unmatched_is_no_matches(monkeypatch):
    _one_feed(monkeypatch)

    def _alive(url):
        if "news.google.com" in url:      # search feed found nothing either
            return _Feed([], title="Google News")
        return _Feed([{"title": "Tesla cuts prices", "link": "u",
                       "published": _iso(1)}])
    monkeypatch.setattr(news_service, "_parse_feed", _alive)

    r = news_service.fetch_news_summary(symbol="KGH", category="stocks", limit=5)

    assert r["status"] == "no_matches"
    assert r["count"] == 0
    assert "error" not in r
    assert r["sources"]["ok"] == ["Google News (KGH)", "Fake Source"]


def test_partial_failure_is_flagged_alongside_items(monkeypatch):
    monkeypatch.setattr(news_service, "RSS_FEEDS", {"stocks": [
        {"url": "good", "name": "Good Feed"},
        {"url": "bad", "name": "Bad Feed"},
    ]})

    def _mixed(url):
        if url == "bad":
            raise urllib.error.HTTPError("u", 500, "boom", {}, None)
        return _Feed([{"title": "Apple beats", "link": "u", "published": _iso(0.5)}])
    monkeypatch.setattr(news_service, "_parse_feed", _mixed)

    r = news_service.fetch_news_summary(category="stocks", limit=5)

    assert r["status"] == "partial"
    assert r["count"] == 1
    assert r["sources"]["ok"] == ["Good Feed"]
    assert r["sources"]["failed"] == {"Bad Feed": "http_500"}


def test_healthy_run_is_ok(monkeypatch):
    _one_feed(monkeypatch)
    monkeypatch.setattr(news_service, "_parse_feed",
                        lambda url: _Feed([{"title": "Apple beats", "link": "u",
                                            "published": _iso(0.5)}]))

    r = news_service.fetch_news_summary(category="stocks", limit=5)
    assert r["status"] == "ok" and r["count"] == 1 and r["sources"]["failed"] == {}


# ── Per-symbol search feed ────────────────────────────────────────────────────

def test_symbol_query_adds_a_search_feed_and_skips_refiltering(monkeypatch):
    """Google News already answered the ticker question — don't re-filter it.

    "Apple beats on iPhone" is an AAPL story even though the ticker never
    appears in the headline; the old substring filter dropped exactly these.
    """
    _one_feed(monkeypatch)
    seen: list[str] = []

    def _capture(url):
        seen.append(url)
        if "news.google.com" in url:
            return _Feed([{"title": "Apple beats on iPhone", "link": "g",
                           "published": _iso(0.2)}], title="Google News")
        return _Feed([{"title": "Tesla cuts prices", "link": "u", "published": _iso(1)}])
    monkeypatch.setattr(news_service, "_parse_feed", _capture)

    r = news_service.fetch_news_summary(symbol="AAPL", category="stocks", limit=5)

    assert any("news.google.com/rss/search?q=AAPL%20stock" in u for u in seen)
    assert r["status"] == "ok"
    assert [i["title"] for i in r["items"]] == ["Apple beats on iPhone"]


def test_polish_symbol_feed_queries_the_company_name():
    """The name headlines print, scoped to markets, not the 3-letter GPW code.

    Bare "KGHM" returns football coverage of the club the company sponsors.
    """
    feed = news_service._symbol_feed("KGH", "pl_stocks")
    assert "ceid=PL:pl" in feed["url"]
    assert "q=KGHM%20akcje&" in feed["url"]
    assert feed["prefiltered"] is True


def test_polish_symbol_feed_falls_back_to_the_ticker_when_unknown():
    feed = news_service._symbol_feed("ZZZ", "pl_stocks")
    assert "q=ZZZ%20akcje&" in feed["url"]


def test_parse_feed_rejects_a_body_it_cannot_parse(monkeypatch):
    """A 200 that isn't a feed is a failure, not an empty news day."""
    class _Bozo:
        entries: list = []
        def get(self, key, default=None):
            return {"bozo": 1}.get(key, default)

    class _Resp:
        def read(self):
            return b"<html>nope</html>"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    monkeypatch.setattr(news_service, "build_opener_with_proxy", lambda ua: _Opener())
    monkeypatch.setattr(news_service.feedparser, "parse", lambda raw: _Bozo())

    try:
        news_service._parse_feed("http://x")
    except ValueError as e:
        assert "unparseable feed" in str(e)
    else:
        raise AssertionError("expected a ValueError for an unparseable body")
