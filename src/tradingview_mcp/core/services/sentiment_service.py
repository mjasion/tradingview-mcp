"""
Market Sentiment Service via Reddit JSON API, with an ApeWisdom fallback.

Uses Agent-Reach architecture (same approach as agent_reach/channels/reddit.py)
but integrated directly into tradingview-mcp as a native service.

**Never returns a fabricated reading.** Reddit answers 403 to server traffic
(verified 2026-08-30 — direct and via proxy), and until then every fetch error
was swallowed into an empty list, so the tool reported ``sentiment_score: 0.0``
/ ``sentiment_label: "Neutral"`` / ``posts_analyzed: 0`` — a failure shaped
exactly like a genuine neutral reading. Now every response carries a
``status``:

* ``ok`` / ``partial`` — real posts were scored (``partial`` = some subreddits
  failed; see ``sources_failed``).
* ``degraded``     — no posts, but ApeWisdom answered with mention *volume*.
  ``sentiment_score`` is ``None`` and the label is ``MENTIONS_ONLY``.
* ``no_data``      — Reddit answered but nothing matched the symbol.
* ``unavailable``  — every source failed. ``sentiment_score`` is ``None``,
  the label is ``UNAVAILABLE`` and ``error`` says why.

``sentiment_score`` is a float only under ``ok``/``partial``; it is ``None``
everywhere else, so "no data" can never be transcribed as "neutral".

Zero external dependencies — pure Python stdlib (urllib, json).
feedparser is optional (used by news_service.py).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

from tradingview_mcp.core.services.apewisdom_service import fetch_mentions
from tradingview_mcp.core.services.log import get_logger
from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy, is_proxy_configured

_log = get_logger("reddit")

# ─── Constants ────────────────────────────────────────────────────────────────

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_TIMEOUT = 10

BULLISH_KEYWORDS = [
    "buy", "bull", "moon", "pump", "long", "call", "up", "gain",
    "strong", "breakout", "bullish", "rally", "surge", "upside",
    "accumulate", "undervalued", "support", "bottom", "recovery",
]

BEARISH_KEYWORDS = [
    "sell", "bear", "dump", "short", "put", "down", "loss", "weak",
    "crash", "drop", "bearish", "tank", "decline", "downside",
    "overvalued", "resistance", "top", "overbought", "bubble",
]

# Subreddit groups by asset class
SUBREDDIT_GROUPS: dict[str, list[str]] = {
    "crypto": ["CryptoCurrency", "Bitcoin", "ethereum", "CryptoMarkets", "altcoin"],
    "stocks": ["stocks", "investing", "wallstreetbets", "StockMarket", "ValueInvesting"],
    "all":    ["wallstreetbets", "stocks", "investing", "CryptoCurrency", "StockMarket"],
}


# ─── Internal helpers ─────────────────────────────────────────────────────────

# ─── Reddit circuit breaker ───────────────────────────────────────────────────
# Reddit refuses unauthenticated .json requests as a matter of policy (OAuth
# only since 2023), so when it fails it fails for every subreddit, every call —
# and the failures are not cheap: a sweep of five subreddits burns ~34s, mostly
# in 10s timeouts, before the ApeWisdom fallback answers in milliseconds. That
# is charged to every market_sentiment and combined_analysis call and eats the
# MCP client's tool timeout.
#
# So one failed sweep opens the circuit: subsequent calls skip Reddit and go
# straight to the fallback, saying so in `sources_failed` and `reddit_circuit`
# rather than pretending five requests were made. The first call after the TTL
# probes again, so access returning (OAuth credentials, a policy change) heals
# the tool on its own. Set TRADINGVIEW_MCP_REDDIT_BREAKER_TTL=0 to disable.
_BREAKER_TTL = int(os.environ.get("TRADINGVIEW_MCP_REDDIT_BREAKER_TTL", 6 * 3600))

_breaker_lock = threading.Lock()
_breaker: dict | None = None


def _breaker_open() -> Optional[dict]:
    """Breaker state while Reddit is known-blocked, else ``None``."""
    if _BREAKER_TTL <= 0:
        return None
    with _breaker_lock:
        if _breaker and time.monotonic() < _breaker["expires_at"]:
            return dict(_breaker)
    return None


def _trip_breaker(reasons: dict[str, str]) -> dict:
    """Record that a full sweep failed, and for how long to stop trying."""
    global _breaker
    now = datetime.now(timezone.utc)
    state = {
        "expires_at": time.monotonic() + _BREAKER_TTL,
        "since": now.isoformat(timespec="seconds"),
        "retry_after": (now + timedelta(seconds=_BREAKER_TTL)).isoformat(timespec="seconds"),
        "reasons": ", ".join(sorted(set(reasons.values()))) or "unknown",
    }
    with _breaker_lock:
        _breaker = state
    return state


def _reset_breaker() -> None:
    """Reddit answered — close the circuit. Also the test hook."""
    global _breaker
    with _breaker_lock:
        _breaker = None


class RedditFetchError(RuntimeError):
    """One subreddit search failed. ``reason`` is a short machine-readable tag.

    Raised instead of returning ``[]`` so a blocked upstream can never be
    mistaken for "no posts about this ticker".
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _fetch_reddit_posts(subreddit: str, query: str, limit: int = 10) -> list:
    """Fetch posts from a subreddit search. Returns raw Reddit post data list.

    Raises:
        RedditFetchError: on any upstream failure (HTTP status, network error,
            timeout, unparseable body).
    """
    url = (
        f"https://www.reddit.com/r/{subreddit}/search.json"
        f"?q={urllib.parse.quote(query)}&sort=new&t=week&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        opener = build_opener_with_proxy(_USER_AGENT)
        with opener.open(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["data"]["children"]
    except urllib.error.HTTPError as e:
        raise RedditFetchError(f"http_{e.code}") from e
    except urllib.error.URLError as e:
        reason = "timeout" if isinstance(e.reason, socket.timeout) else str(e.reason)
        raise RedditFetchError(f"unreachable: {reason}") from e
    except socket.timeout as e:
        raise RedditFetchError("timeout") from e
    except (ValueError, KeyError, TypeError) as e:
        raise RedditFetchError(f"parse_error: {type(e).__name__}") from e
    except Exception as e:
        raise RedditFetchError(f"error: {type(e).__name__}") from e


def _score_text(text: str) -> float:
    """
    Score a text for bullish/bearish sentiment.
    Returns float from -1.0 (fully bearish) to +1.0 (fully bullish).
    0.0 = neutral or no sentiment words found.
    """
    t = text.lower()
    bull = sum(1 for w in BULLISH_KEYWORDS if w in t)
    bear = sum(1 for w in BEARISH_KEYWORDS if w in t)
    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / total


def _label(score: float) -> str:
    if score > 0.2:
        return "Strongly Bullish"
    elif score > 0.05:
        return "Bullish"
    elif score < -0.2:
        return "Strongly Bearish"
    elif score < -0.05:
        return "Bearish"
    return "Neutral"


# ─── Public API ───────────────────────────────────────────────────────────────

def analyze_sentiment(
    symbol: str,
    category: str = "all",
    limit: int = 20,
) -> dict:
    """
    Analyze Reddit sentiment for a given symbol.

    Args:
        symbol:   Asset ticker/name ("AAPL", "BTC", "ETH", "TSLA", "THYAO")
        category: Subreddit group — "crypto" | "stocks" | "all"
        limit:    Total number of posts to analyze across all subreddits

    Returns:
        dict with a ``status`` (``ok`` | ``partial`` | ``degraded`` |
        ``no_data`` | ``unavailable``). ``sentiment_score`` is a float in
        -1..+1 **only** for ``ok``/``partial``; otherwise it is ``None`` and
        ``sentiment_label`` is ``MENTIONS_ONLY`` / ``NO_DATA`` /
        ``UNAVAILABLE``. Failed subreddits are listed in ``sources_failed``.
    """
    subs = SUBREDDIT_GROUPS.get(category, SUBREDDIT_GROUPS["all"])
    per_sub = max(2, limit // len(subs) + 1)

    all_posts: list[dict] = []
    scores: list[float] = []
    # Reddit's per-subreddit search occasionally returns the same post in
    # multiple subreddits (cross-posts) or twice within one feed. Dedup by
    # permalink so a single post does not double-count toward bullish/bearish
    # tallies or appear twice in top_posts.
    seen_permalinks: set[str] = set()
    skipped_dupes = 0
    sources_ok: list[str] = []
    sources_failed: dict[str, str] = {}

    breaker = _breaker_open()
    skipped = bool(breaker)   # this call skipped; a trip below is not the same
    if breaker:
        # Say it was skipped. Reporting five fresh 403s we never sent would be
        # a different lie than the one this module exists to stop.
        sources_failed["reddit"] = f"circuit_open: {breaker['reasons']}"
        _log.info("Reddit skipped for %s — known blocked since %s (%s), retrying after %s",
                  symbol.upper(), breaker["since"], breaker["reasons"], breaker["retry_after"])

    if not breaker:
        _log.info("scanning Reddit for %s across %d subreddits (%s)",
                  symbol.upper(), len(subs), ", ".join(f"r/{s}" for s in subs[:4]))

    for sub in (() if breaker else subs):
        try:
            raw = _fetch_reddit_posts(sub, symbol, per_sub)
        except RedditFetchError as e:
            sources_failed[f"r/{sub}"] = e.reason
            _log.warning("r/%s search failed: %s", sub, e.reason)
            continue
        sources_ok.append(f"r/{sub}")
        _log.debug("r/%s: %d raw posts", sub, len(raw))
        for p in raw:
            d = p.get("data", {})
            permalink = d.get("permalink", "")
            dedup_key = permalink or d.get("id", "")
            if dedup_key and dedup_key in seen_permalinks:
                skipped_dupes += 1
                continue
            if dedup_key:
                seen_permalinks.add(dedup_key)
            title = d.get("title", "")
            body = d.get("selftext", "")
            text = f"{title} {body}"
            score = _score_text(text)
            scores.append(score)
            all_posts.append({
                "title": title[:120],
                "upvotes": d.get("score", 0),
                "comments": d.get("num_comments", 0),
                "sentiment": "bullish" if score > 0 else "bearish" if score < 0 else "neutral",
                "url": f"https://reddit.com{permalink}",
                "subreddit": f"r/{sub}",
            })

    if sources_ok:
        _reset_breaker()          # Reddit answered — undo any earlier trip
    elif not breaker:
        breaker = _trip_breaker(sources_failed)
        _log.warning("Reddit circuit opened for %ds after a full sweep failed (%s)",
                     _BREAKER_TTL, breaker["reasons"])

    base = {
        "symbol": symbol.upper(),
        "posts_analyzed": len(scores),
        "bullish_count": sum(1 for s in scores if s > 0),
        "bearish_count": sum(1 for s in scores if s < 0),
        "neutral_count": sum(1 for s in scores if s == 0),
        "sources": [f"r/{s}" for s in subs],
        "sources_ok": sources_ok,
        "sources_failed": sources_failed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if breaker:
        base["reddit_circuit"] = {
            "state": "open",
            "since": breaker["since"],
            "retry_after": breaker["retry_after"],
            "reasons": breaker["reasons"],
        }

    # ── Real posts: score them ────────────────────────────────────────────
    if scores:
        avg = sum(scores) / len(scores)
        all_posts.sort(key=lambda x: x["upvotes"], reverse=True)
        _log.info("Reddit %s: %d posts analysed, %d cross-posts deduped — sentiment %.2f (%s)",
                  symbol.upper(), len(scores), skipped_dupes, avg, _label(avg))
        return {
            **base,
            "status": "partial" if sources_failed else "ok",
            "data_source": "reddit",
            "sentiment_score": round(avg, 3),
            "sentiment_label": _label(avg),
            "top_posts": all_posts[:5],
        }

    # ── No posts. Never call that "Neutral" — say what actually happened ──
    reddit_down = not sources_ok
    if skipped:
        _log.info("Reddit not queried for %s (circuit open); using ApeWisdom",
                  symbol.upper())
    elif reddit_down:
        _log.warning("Reddit unusable for %s — every subreddit failed (%s); trying ApeWisdom",
                     symbol.upper(), ", ".join(sorted(set(sources_failed.values()))))
    else:
        _log.info("Reddit %s: 0 matching posts across %d reachable subreddits; trying ApeWisdom",
                  symbol.upper(), len(sources_ok))

    mentions = fetch_mentions(symbol, category=category)
    empty = {**base, "sentiment_score": None, "top_posts": [], "mentions": mentions}

    if mentions.get("status") in ("ok", "not_ranked"):
        return {
            **empty,
            "status": "degraded",
            "data_source": "apewisdom",
            "sentiment_label": "MENTIONS_ONLY",
            "reason": (
                f"Reddit skipped — known blocked since {breaker['since']} "
                f"({breaker['reasons']}), next attempt after {breaker['retry_after']}; "
                "falling back to ApeWisdom mention volume."
                if skipped else
                "Reddit search is unreachable from this server (403 to datacenter "
                "traffic); falling back to ApeWisdom mention volume."
                if reddit_down else
                "Reddit search returned no posts for this symbol; ApeWisdom "
                "mention volume attached instead."
            ),
            "recommended_action": (
                "Use `mentions` (mention volume and its 24h delta) — there is NO "
                "bullish/bearish reading here. Do not record a directional sentiment."
            ),
        }

    if reddit_down:
        return {
            **empty,
            "status": "unavailable",
            "data_source": None,
            "sentiment_label": "UNAVAILABLE",
            "error": "reddit_blocked_and_fallback_failed",
            "reason": (
                (f"Reddit skipped — known blocked since {breaker['since']} "
                 f"({breaker['reasons']})" if skipped else
                 f"Every Reddit search failed ({', '.join(sorted(set(sources_failed.values())))})")
                + f" and the ApeWisdom fallback also failed ({mentions.get('error')})."
            ),
            "recommended_action": (
                "Treat the sentiment pillar as MISSING, not neutral. Mark it "
                "`[MCP error]` and source it elsewhere before relying on it."
            ),
        }

    return {
        **empty,
        "status": "no_data",
        "data_source": "reddit",
        "sentiment_label": "NO_DATA",
        "reason": (
            f"{len(sources_ok)} subreddit(s) answered but no post mentioned "
            f"{symbol.upper()} in the last week; ApeWisdom fallback also failed "
            f"({mentions.get('error')})."
        ),
        "recommended_action": (
            "No sample to score. This is an absence of data, not a neutral reading."
        ),
    }
