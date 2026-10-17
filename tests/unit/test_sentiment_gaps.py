"""A missing sentiment reading must never look like a neutral one.

Reddit answers 403 to this server (verified 2026-08-30), and the old code
swallowed that into ``sentiment_score: 0.0`` / ``"Neutral"`` — indistinguishable
from a real neutral reading. These tests pin the honest contract.
"""
from __future__ import annotations

import urllib.error

import pytest

from tradingview_mcp.core.services import sentiment_service
from tradingview_mcp.core.services.sentiment_service import RedditFetchError


def _post(permalink: str, title: str = "AAPL strong buy bullish") -> dict:
    return {"data": {
        "permalink": permalink, "id": permalink, "title": title,
        "selftext": "", "score": 10, "num_comments": 2,
    }}


def _block_reddit(monkeypatch, reason: str = "http_403"):
    def _boom(sub, query, limit):
        raise RedditFetchError(reason)
    monkeypatch.setattr(sentiment_service, "_fetch_reddit_posts", _boom)


def _mentions(monkeypatch, payload: dict):
    monkeypatch.setattr(sentiment_service, "fetch_mentions",
                        lambda symbol, category="all": payload)


# ── Reddit blocked ────────────────────────────────────────────────────────────

def test_blocked_reddit_never_reports_neutral(monkeypatch):
    """The exact bug: every subreddit 403s, fallback also dead."""
    _block_reddit(monkeypatch)
    _mentions(monkeypatch, {"status": "unavailable", "error": "apewisdom_http_503"})

    r = sentiment_service.analyze_sentiment("AAPL", category="stocks")

    assert r["status"] == "unavailable"
    assert r["sentiment_score"] is None
    assert r["sentiment_label"] == "UNAVAILABLE"
    assert r["sentiment_label"] != "Neutral"
    assert r["error"] == "reddit_blocked_and_fallback_failed"
    assert set(r["sources_failed"]) == {f"r/{s}" for s in
                                        sentiment_service.SUBREDDIT_GROUPS["stocks"]}
    assert all(v == "http_403" for v in r["sources_failed"].values())
    assert r["sources_ok"] == []
    assert "recommended_action" in r


def test_blocked_reddit_falls_back_to_mention_volume(monkeypatch):
    """ApeWisdom answers: volume only, still no directional score."""
    _block_reddit(monkeypatch, "unreachable: timed out")
    _mentions(monkeypatch, {
        "status": "ok", "source": "apewisdom", "ticker": "AAPL",
        "rank": 37, "mentions_24h": 5, "mentions_prev_24h": 30,
        "mentions_change_pct": -83.3,
    })

    r = sentiment_service.analyze_sentiment("AAPL", category="stocks")

    assert r["status"] == "degraded"
    assert r["data_source"] == "apewisdom"
    assert r["sentiment_score"] is None
    assert r["sentiment_label"] == "MENTIONS_ONLY"
    assert r["mentions"]["mentions_24h"] == 5
    assert r["posts_analyzed"] == 0


def test_fallback_not_ranked_is_a_reading_not_a_gap(monkeypatch):
    """A ticker nobody talks about (TEAM) is data, and must be labelled as such."""
    _block_reddit(monkeypatch)
    _mentions(monkeypatch, {"status": "not_ranked", "ticker": "TEAM",
                            "mentions_24h": 0, "scanned_top": 300})

    r = sentiment_service.analyze_sentiment("TEAM", category="stocks")

    assert r["status"] == "degraded"
    assert r["sentiment_label"] == "MENTIONS_ONLY"
    assert r["mentions"]["mentions_24h"] == 0
    assert r["mentions"]["scanned_top"] == 300


# ── Reddit reachable ──────────────────────────────────────────────────────────

def test_reachable_but_no_posts_is_no_data_not_neutral(monkeypatch):
    monkeypatch.setattr(sentiment_service, "_fetch_reddit_posts",
                        lambda sub, query, limit: [])
    _mentions(monkeypatch, {"status": "unavailable", "error": "apewisdom_http_500"})

    r = sentiment_service.analyze_sentiment("AAPL", category="stocks")

    assert r["status"] == "no_data"
    assert r["sentiment_score"] is None
    assert r["sentiment_label"] == "NO_DATA"
    assert len(r["sources_ok"]) == len(sentiment_service.SUBREDDIT_GROUPS["stocks"])
    assert r["sources_failed"] == {}


def test_partial_failure_keeps_the_score_and_names_the_casualties(monkeypatch):
    """Some subreddits answering is enough to score — but say which didn't."""
    def _some(sub, query, limit):
        if sub == "stocks":
            return [_post("/r/stocks/a")]
        raise RedditFetchError("http_429")
    monkeypatch.setattr(sentiment_service, "_fetch_reddit_posts", _some)

    r = sentiment_service.analyze_sentiment("AAPL", category="stocks")

    assert r["status"] == "partial"
    assert r["data_source"] == "reddit"
    assert isinstance(r["sentiment_score"], float)
    assert r["posts_analyzed"] == 1
    assert r["sources_ok"] == ["r/stocks"]
    assert "r/investing" in r["sources_failed"]


def test_clean_run_reports_ok(monkeypatch):
    monkeypatch.setattr(sentiment_service, "_fetch_reddit_posts",
                        lambda sub, query, limit: [_post(f"/r/{sub}/a")])

    r = sentiment_service.analyze_sentiment("AAPL", category="stocks")

    assert r["status"] == "ok"
    assert r["sources_failed"] == {}
    assert r["sentiment_score"] > 0
    assert r["sentiment_label"] in ("Bullish", "Strongly Bullish")


# ── Fetch-level error classification ──────────────────────────────────────────

@pytest.mark.parametrize("exc, expected", [
    (urllib.error.HTTPError("u", 403, "Blocked", {}, None), "http_403"),
    (urllib.error.URLError("nodename nor servname"), "unreachable: nodename nor servname"),
    (ValueError("bad json"), "parse_error: ValueError"),
])
def test_fetch_errors_are_raised_with_a_reason(monkeypatch, exc, expected):
    class _Opener:
        def open(self, req, timeout=None):
            raise exc
    monkeypatch.setattr(sentiment_service, "build_opener_with_proxy",
                        lambda ua: _Opener())

    with pytest.raises(RedditFetchError) as err:
        sentiment_service._fetch_reddit_posts("stocks", "AAPL", 5)
    assert err.value.reason == expected


# ── Circuit breaker ───────────────────────────────────────────────────────────

def _count_attempts(monkeypatch) -> list:
    """Patch the fetch to fail, recording every subreddit actually attempted."""
    attempts: list[str] = []

    def _boom(sub, query, limit):
        attempts.append(sub)
        raise RedditFetchError("http_403")
    monkeypatch.setattr(sentiment_service, "_fetch_reddit_posts", _boom)
    return attempts


def test_second_call_skips_reddit_instead_of_burning_the_timeout(monkeypatch):
    """A failed sweep costs ~34s of timeouts; don't pay it on every call."""
    attempts = _count_attempts(monkeypatch)
    _mentions(monkeypatch, {"status": "ok", "mentions_24h": 5, "rank": 37})
    subs = len(sentiment_service.SUBREDDIT_GROUPS["stocks"])

    first = sentiment_service.analyze_sentiment("AAPL", category="stocks")
    assert len(attempts) == subs

    second = sentiment_service.analyze_sentiment("MSFT", category="stocks")

    assert len(attempts) == subs                      # nothing new was tried
    assert second["status"] == "degraded"             # still answers, via fallback
    assert first["status"] == "degraded"


def test_a_skipped_sweep_says_it_was_skipped(monkeypatch):
    """Reporting five fresh 403s we never sent would be its own lie."""
    _count_attempts(monkeypatch)
    _mentions(monkeypatch, {"status": "ok", "mentions_24h": 5, "rank": 37})

    sentiment_service.analyze_sentiment("AAPL", category="stocks")
    r = sentiment_service.analyze_sentiment("AAPL", category="stocks")

    assert r["sources_failed"] == {"reddit": "circuit_open: http_403"}
    assert r["sources_ok"] == []
    circuit = r["reddit_circuit"]
    assert circuit["state"] == "open"
    assert circuit["reasons"] == "http_403"
    assert circuit["since"] < circuit["retry_after"]
    assert "skipped" in r["reason"] and "known blocked" in r["reason"]


def test_circuit_reopens_for_a_probe_once_the_ttl_expires(monkeypatch):
    """Self-healing: access coming back must not need a restart."""
    attempts = _count_attempts(monkeypatch)
    _mentions(monkeypatch, {"status": "ok", "mentions_24h": 5, "rank": 37})
    subs = len(sentiment_service.SUBREDDIT_GROUPS["stocks"])

    sentiment_service.analyze_sentiment("AAPL", category="stocks")
    monkeypatch.setattr(sentiment_service.time, "monotonic",
                        lambda: sentiment_service._breaker["expires_at"] + 1)

    sentiment_service.analyze_sentiment("AAPL", category="stocks")
    assert len(attempts) == 2 * subs


def test_a_working_subreddit_closes_the_circuit(monkeypatch):
    monkeypatch.setattr(sentiment_service, "_fetch_reddit_posts",
                        lambda sub, query, limit: [_post(f"/r/{sub}/a")])

    r = sentiment_service.analyze_sentiment("AAPL", category="stocks")

    assert r["status"] == "ok"
    assert "reddit_circuit" not in r
    assert sentiment_service._breaker_open() is None


def test_breaker_can_be_disabled(monkeypatch):
    """TRADINGVIEW_MCP_REDDIT_BREAKER_TTL=0 restores try-every-time."""
    monkeypatch.setattr(sentiment_service, "_BREAKER_TTL", 0)
    attempts = _count_attempts(monkeypatch)
    _mentions(monkeypatch, {"status": "ok", "mentions_24h": 5, "rank": 37})
    subs = len(sentiment_service.SUBREDDIT_GROUPS["stocks"])

    sentiment_service.analyze_sentiment("AAPL", category="stocks")
    sentiment_service.analyze_sentiment("AAPL", category="stocks")

    assert len(attempts) == 2 * subs


def test_the_sweep_that_trips_the_breaker_still_reports_its_own_403s(monkeypatch):
    """Call #1 did try — only calls after it are 'skipped'."""
    _count_attempts(monkeypatch)
    _mentions(monkeypatch, {"status": "ok", "mentions_24h": 5, "rank": 37})

    r = sentiment_service.analyze_sentiment("AAPL", category="stocks")

    assert "skipped" not in r["reason"]
    assert set(r["sources_failed"]) == {f"r/{s}" for s in
                                        sentiment_service.SUBREDDIT_GROUPS["stocks"]}
    assert r["reddit_circuit"]["state"] == "open"   # tripped for the next call
