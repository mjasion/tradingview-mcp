"""
Financial News Service via RSS feeds.

Uses feedparser (already installed as part of agent-reach dependencies).
No API keys required. Pulls from free, public RSS feeds.

Sources:
  crypto:    CoinDesk, CoinTelegraph
  stocks:    MarketWatch, CNBC, Investing.com, Seeking Alpha
  pl_stocks: Bankier, Money.pl, Comparic, StockWatch + PAP Biznes (scraper)
  all:       Combined
  per-symbol: a Google News search feed is appended whenever ``symbol`` is
             given — the general feeds rarely name a ticker, which is why
             per-ticker queries used to come back empty.

Two failure modes are reported, never swallowed (see ``fetch_news_summary``):
a feed that fails is listed in ``sources.failed``, and a symbol query that
matched nothing returns ``status="no_matches"`` — distinct from
``status="unavailable"`` when every feed is down. ``count: 0`` alone never
means "no news happened".

Feeds are fetched through the proxy-aware opener with a browser User-Agent:
CNBC answers 403 to feedparser's own UA, and going through the opener also
routes the request via the egress proxy and the global rate limiter.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from tradingview_mcp.core.services.log import get_logger
from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy

# feedparser is bundled with agent-reach (installed globally)
try:
    import feedparser
    _FEEDPARSER_AVAILABLE = True
except ImportError:
    _FEEDPARSER_AVAILABLE = False

_log = get_logger("news")

# ─── Feed Catalog ─────────────────────────────────────────────────────────────

RSS_FEEDS: dict[str, list[dict]] = {
    "crypto": [
        {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "name": "CoinDesk"},
        {"url": "https://cointelegraph.com/rss", "name": "CoinTelegraph"},
    ],
    # feeds.reuters.com is NXDOMAIN (Reuters retired its public RSS) — it was
    # the only source here, so `stocks` silently returned zero items for every
    # query. Replacements verified live 2026-08-30.
    "stocks": [
        {"url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "name": "MarketWatch Top Stories"},
        {"url": "https://www.cnbc.com/id/10000664/device/rss/rss.html", "name": "CNBC Finance"},
        {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "name": "CNBC Top News"},
        {"url": "https://www.investing.com/rss/news_25.rss", "name": "Investing.com Stock Market"},
        {"url": "https://seekingalpha.com/market_currents.xml", "name": "Seeking Alpha Market Currents"},
    ],
    "all": [
        {"url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "name": "MarketWatch Top Stories"},
        {"url": "https://www.cnbc.com/id/10000664/device/rss/rss.html", "name": "CNBC Finance"},
        {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "name": "CoinDesk"},
        {"url": "https://cointelegraph.com/rss", "name": "CoinTelegraph"},
    ],
    # Polish stock market. RSS feeds verified live (May 2026); PAP Biznes is
    # added as an HTML scraper (see pap_scraper.py) because biznes.pap.pl
    # exposes no public RSS. Verified MISS / no public RSS: parkiet.com,
    # biznesradar.pl, stooq.pl/n, forsal, wnp, strefainwestorow, rp.pl/biznes,
    # gpwalert.com, infostrefa.com (cert expired), pb.pl (HTML at /rss),
    # gpw.pl (HTML at /rss).
    "pl_stocks": [
        {"url": "https://www.bankier.pl/rss/wiadomosci.xml",   "name": "Bankier.pl"},
        {"url": "https://www.money.pl/rss/gielda.xml",         "name": "Money.pl Giełda"},
        {"url": "https://www.money.pl/rss/news.xml",           "name": "Money.pl"},
        {"url": "https://comparic.pl/feed/",                   "name": "Comparic.pl"},
        {"url": "https://www.stockwatch.pl/wiadomosci/feed",   "name": "StockWatch.pl"},
        # PAP Biznes — HTML scraper, attached after the RSS loop in fetch_news()
    ],
}

_TIMEOUT = 8
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ─── Public API ───────────────────────────────────────────────────────────────

def _symbol_search_terms(symbol: str, category: str) -> list[str]:
    """Return list of substrings to look for. For Polish category, expand the
    ticker to common company-name aliases (KGH → ['KGHM','KGH','Polska Miedź']).
    """
    if category == "pl_stocks":
        from tradingview_mcp.core.data.gpw_company_names import search_aliases
        return [a.upper() for a in search_aliases(symbol)]
    return [symbol.upper()]


def _symbol_feed(symbol: str, category: str) -> dict:
    """A Google News search feed scoped to *symbol*.

    The general business feeds carry maybe a hundred headlines a day and
    rarely name a mid-cap, so a symbol filter over them returns nothing for
    most tickers. A search feed asks the question directly. Results come back
    pre-filtered by the query, so ``prefiltered`` tells the caller to skip the
    substring match (a story titled "Apple beats on iPhone" is about AAPL even
    though the ticker never appears).
    """
    if category == "pl_stocks":
        from tradingview_mcp.core.data.gpw_company_names import search_aliases
        aliases = search_aliases(symbol)
        # First alias is the name headlines actually use (KGH → "KGHM",
        # CDR → "CD Projekt"); the bare ticker matches too much unrelated
        # text, and the longest alias is often a secondary name
        # ("Polska Miedź") that fewer outlets print.
        # "akcje" is the PL analogue of the "stock" suffix below: without it
        # "KGHM" returns football coverage of KGHM Zagłębie Lubin, the club
        # the company sponsors.
        query = f"{aliases[0] if aliases else symbol} akcje"
        locale = "hl=pl&gl=PL&ceid=PL:pl"
        name = "Google News PL"
    else:
        query = f"{symbol.upper()} stock"
        locale = "hl=en-US&gl=US&ceid=US:en"
        name = "Google News"
    return {
        "url": f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&{locale}",
        "name": f"{name} ({symbol.upper()})",
        "prefiltered": True,
    }


def _parse_feed(url: str):
    """Fetch *url* through the proxy opener and hand the bytes to feedparser.

    Raises on transport failure so the caller can record which feed died —
    ``feedparser.parse(url)`` swallows HTTP errors into an empty entry list,
    which is what made dead feeds look like quiet news days.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    })
    opener = build_opener_with_proxy(_UA)
    with opener.open(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    parsed = feedparser.parse(raw)
    if not parsed.entries and parsed.get("bozo"):
        raise ValueError(f"unparseable feed: {type(parsed.get('bozo_exception')).__name__}")
    return parsed


# Every symbol in a portfolio_scan re-reads the same general feeds; within a
# few minutes they are byte-identical. Remember each feed's outcome briefly —
# successes for _FEED_TTL, failures for _FEED_FAIL_TTL so a dead feed's 10s
# timeout is paid once, not once per symbol. A per-URL lock makes concurrent
# workers wait for the one fetch in flight instead of each starting their own.
def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


_FEED_TTL = _float_env("TRADINGVIEW_MCP_NEWS_FEED_TTL", 300.0)
_FEED_FAIL_TTL = min(_FEED_TTL, 60.0)
_FEED_CACHE: dict[str, tuple[float, object, Optional[Exception]]] = {}
_FEED_LOCKS: dict[str, threading.Lock] = {}
_FEED_LOCKS_GUARD = threading.Lock()


def _get_feed(url: str):
    """``_parse_feed`` behind a short per-URL memo. Re-raises a remembered
    failure, so the caller still records that feed as failed."""
    if _FEED_TTL <= 0:
        return _parse_feed(url)
    with _FEED_LOCKS_GUARD:
        lock = _FEED_LOCKS.setdefault(url, threading.Lock())
    with lock:
        entry = _FEED_CACHE.get(url)
        if entry is not None:
            stored_at, feed, err = entry
            if time.monotonic() - stored_at < (_FEED_FAIL_TTL if err else _FEED_TTL):
                _log.debug("feed cache hit: %s", url)
                if err is not None:
                    raise err
                return feed
        try:
            feed = _parse_feed(url)
        except Exception as e:
            _FEED_CACHE[url] = (time.monotonic(), None, e)
            raise
        if len(_FEED_CACHE) >= 256:
            _FEED_CACHE.clear()
        _FEED_CACHE[url] = (time.monotonic(), feed, None)
        return feed


def reset_feed_cache_for_tests() -> None:
    with _FEED_LOCKS_GUARD:
        _FEED_CACHE.clear()
        _FEED_LOCKS.clear()


def _feed_error(e: Exception) -> str:
    """Short machine-readable tag for a failed feed."""
    if isinstance(e, urllib.error.HTTPError):
        return f"http_{e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"unreachable: {e.reason}"
    return f"{type(e).__name__}: {e}"


def fetch_news(
    symbol: Optional[str] = None,
    category: str = "stocks",
    limit: int = 10,
) -> list[dict]:
    """Fetch financial news from RSS feeds. See :func:`fetch_news_with_health`.

    Returns just the items; callers that need to tell "no news matched" from
    "every feed is down" should use :func:`fetch_news_with_health` instead.
    """
    return fetch_news_with_health(symbol, category, limit)[0]


def fetch_news_with_health(
    symbol: Optional[str] = None,
    category: str = "stocks",
    limit: int = 10,
) -> tuple[list[dict], dict]:
    """
    Fetch financial news from RSS feeds, reporting per-feed outcomes.

    Args:
        symbol:   Optional ticker filter. If provided, only returns headlines
                  that mention the symbol (case-insensitive), and a Google News
                  search feed for that symbol is added to the sources. For
                  category ``pl_stocks`` the ticker is also expanded to
                  company-name aliases (``KGH`` matches "KGHM", "Polska Miedź").
        category: Feed group — "crypto" | "stocks" | "pl_stocks" | "all"
        limit:    Maximum number of items to return

    Returns:
        ``(items, health)`` where *health* is
        ``{"attempted": n, "ok": [names], "failed": {name: reason}}``.
    """
    if not _FEEDPARSER_AVAILABLE:
        return [], {
            "attempted": 0, "ok": [], "failed": {},
            "error": "feedparser not installed. Run: pip install feedparser",
        }

    feeds = list(RSS_FEEDS.get(category, RSS_FEEDS["stocks"]))
    search_terms = _symbol_search_terms(symbol, category) if symbol else []
    if symbol:
        feeds.insert(0, _symbol_feed(symbol, category))
    feeds_ok: list[str] = []
    feeds_failed: dict[str, str] = {}
    if symbol:
        _log.info("searching news for %s in '%s' (terms: %s)",
                  symbol.upper(), category, ", ".join(search_terms[:5]))
    else:
        _log.info("fetching news from '%s' feeds (%d sources)", category, len(feeds))
    results: list[dict] = []
    seen_titles: set[tuple[str, str]] = set()
    skipped_dupes = 0
    skipped_stale = 0

    def _try_add(item: dict) -> None:
        """Append *item* unless it's a (title, source) duplicate or stale."""
        nonlocal skipped_dupes, skipped_stale
        key = (item["title"].strip().lower(), item["source"])
        if not item["title"]:
            return
        if key in seen_titles:
            skipped_dupes += 1
            return
        if _is_stale(item["published"]):
            skipped_stale += 1
            return
        seen_titles.add(key)
        results.append(item)

    # Phase 1: collect generously (over-fetch from each feed to give the
    # freshness sort enough material). We only enforce *limit* after sorting.
    fetch_target = max(limit * 3, 30)

    for feed_info in feeds:
        if len(results) >= fetch_target:
            break
        try:
            _log.debug("fetching feed: %s", feed_info["name"])
            feed = _get_feed(feed_info["url"])
        except Exception as e:
            reason = _feed_error(e)
            feeds_failed[feed_info["name"]] = reason
            _log.warning("feed %s failed: %s", feed_info["name"], reason)
            continue

        feeds_ok.append(feed_info["name"])
        source_name = feed.feed.get("title", feed_info["name"])
        # A search feed already answered the ticker question; re-filtering it
        # on the raw ticker would drop stories that name the company instead.
        prefiltered = feed_info.get("prefiltered", False)

        for entry in feed.entries:
            if len(results) >= fetch_target:
                break

            title = entry.get("title", "")
            summary = entry.get("summary", "") or entry.get("description", "")

            if search_terms and not prefiltered:
                combined = f"{title} {summary}".upper()
                if not any(term in combined for term in search_terms):
                    continue

            _try_add({
                "title": title,
                "url": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": _clean_html(summary)[:300],
                "source": source_name,
            })

    # PAP Biznes has no RSS — synthesise feed from HTML scraper for pl_stocks.
    if category == "pl_stocks" and len(results) < fetch_target:
        try:
            from tradingview_mcp.core.services.pap_scraper import fetch_pap_items
            _log.debug("topping up pl_stocks with PAP Biznes scraper")
            for item in fetch_pap_items(limit=15):
                if len(results) >= fetch_target:
                    break
                if search_terms:
                    haystack = f"{item['title']} {item['url']}".upper()
                    if not any(term in haystack for term in search_terms):
                        continue
                _try_add(item)
            feeds_ok.append("PAP Biznes")
        except Exception as e:
            feeds_failed["PAP Biznes"] = _feed_error(e)
            _log.warning("PAP Biznes scraper failed: %s", _feed_error(e))

    # Phase 2: sort by freshness (newest first), then trim to *limit*.
    results.sort(key=lambda i: _parse_published(i["published"]) or _EPOCH, reverse=True)
    final = results[:limit]
    _log.info("news: %d items returned from %d/%d feeds (filtered %d duplicates, %d stale)",
              len(final), len(feeds_ok), len(feeds_ok) + len(feeds_failed),
              skipped_dupes, skipped_stale)
    return final, {
        "attempted": len(feeds_ok) + len(feeds_failed),
        "ok": feeds_ok,
        "failed": feeds_failed,
    }


# ─── Date helpers ─────────────────────────────────────────────────────────────

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_AGE = timedelta(days=7)


def _parse_published(raw: str) -> Optional[datetime]:
    """Parse RSS/PAP date strings into aware UTC datetimes. Returns None on failure."""
    if not raw:
        return None
    # ISO 8601 first (PAP scraper emits this)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = None
    # RFC 2822 (most RSS feeds)
    if dt is None:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_stale(raw: str) -> bool:
    """True if *raw* parses to >7 days old. Items with unparseable dates pass."""
    dt = _parse_published(raw)
    if dt is None:
        return False
    return datetime.now(timezone.utc) - dt > _MAX_AGE


def fetch_news_summary(
    symbol: Optional[str] = None,
    category: str = "stocks",
    limit: int = 10,
) -> dict:
    """
    Fetch news and return structured dict for MCP tool output.

    Carries an explicit ``status`` so ``count: 0`` is never ambiguous:

    * ``ok``          — feeds answered, items returned.
    * ``partial``     — items returned, but some feeds failed.
    * ``no_matches``  — feeds answered; nothing mentioned the symbol.
    * ``no_news``     — feeds answered; they carried nothing fresh at all.
    * ``unavailable`` — every feed failed. ``error`` says so; a zero count here
      means "we could not look", not "nothing happened".
    """
    items, health = fetch_news_with_health(symbol, category, limit)
    out = {
        "symbol": symbol,
        "category": category,
        "count": len(items),
        "feedparser_available": _FEEDPARSER_AVAILABLE,
        "items": items,
        "sources": health,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if health.get("error"):
        return {**out, "status": "unavailable", "error": health["error"]}

    if not health["ok"]:
        return {
            **out,
            "status": "unavailable",
            "error": "all_feeds_failed",
            "note": (
                f"All {health['attempted']} '{category}' feeds failed "
                f"({'; '.join(f'{k}: {v}' for k, v in health['failed'].items())}). "
                f"A count of 0 here means the sources were unreachable, not that "
                f"there is no news."
            ),
            "recommended_action": "Mark the news pillar `[MCP error]` and retry later or use WebSearch.",
        }

    if items:
        if health["failed"]:
            return {**out, "status": "partial",
                    "note": f"{len(health['failed'])} of {health['attempted']} feeds failed; "
                            f"the items below come from the rest."}
        return {**out, "status": "ok"}

    if symbol:
        return {
            **out,
            "status": "no_matches",
            "note": (
                f"{len(health['ok'])} feed(s) answered but nothing published in the "
                f"last 7 days mentions {symbol.upper()}. This is a real absence of "
                f"coverage, not a failed lookup."
            ),
        }
    return {**out, "status": "no_news",
            "note": f"{len(health['ok'])} feed(s) answered with nothing fresh (7-day window)."}


# ─── Utils ────────────────────────────────────────────────────────────────────

def _clean_html(text: str) -> str:
    """Strip basic HTML tags from text."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return text.strip()
