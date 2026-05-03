"""
Financial News Service via RSS feeds.

Uses feedparser (already installed as part of agent-reach dependencies).
No API keys required. Pulls from free, public RSS feeds.

Sources:
  crypto: CoinDesk, Cointelegraph
  stocks: Reuters Business News
  all:    Combined
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

# feedparser is bundled with agent-reach (installed globally)
try:
    import feedparser
    _FEEDPARSER_AVAILABLE = True
except ImportError:
    _FEEDPARSER_AVAILABLE = False

# ─── Feed Catalog ─────────────────────────────────────────────────────────────

RSS_FEEDS: dict[str, list[dict]] = {
    "crypto": [
        {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "name": "CoinDesk"},
        {"url": "https://cointelegraph.com/rss", "name": "CoinTelegraph"},
    ],
    "stocks": [
        {"url": "https://feeds.reuters.com/reuters/businessNews", "name": "Reuters Business"},
        {"url": "https://feeds.reuters.com/reuters/companyNews", "name": "Reuters Company"},
    ],
    "all": [
        {"url": "https://feeds.reuters.com/reuters/businessNews", "name": "Reuters Business"},
        {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "name": "CoinDesk"},
        {"url": "https://cointelegraph.com/rss", "name": "CoinTelegraph"},
    ],
    # Polish stock market. RSS feeds verified live (May 2026); PAP Biznes is
    # added as an HTML scraper (see pap_scraper.py) because biznes.pap.pl
    # exposes no public RSS. Verified MISS / no public RSS: parkiet.com,
    # biznesradar.pl, stooq.pl/n, forsal, wnp, strefainwestorow, rp.pl/biznes.
    "pl_stocks": [
        {"url": "https://www.bankier.pl/rss/wiadomosci.xml", "name": "Bankier.pl"},
        {"url": "https://www.money.pl/rss/gielda.xml",       "name": "Money.pl Giełda"},
        {"url": "https://www.money.pl/rss/news.xml",         "name": "Money.pl"},
        {"url": "https://comparic.pl/feed/",                 "name": "Comparic.pl"},
        # PAP Biznes — HTML scraper, attached after the RSS loop in fetch_news()
    ],
}

_TIMEOUT = 8


# ─── Public API ───────────────────────────────────────────────────────────────

def _symbol_search_terms(symbol: str, category: str) -> list[str]:
    """Return list of substrings to look for. For Polish category, expand the
    ticker to common company-name aliases (KGH → ['KGHM','KGH','Polska Miedź']).
    """
    if category == "pl_stocks":
        from tradingview_mcp.core.data.gpw_company_names import search_aliases
        return [a.upper() for a in search_aliases(symbol)]
    return [symbol.upper()]


def fetch_news(
    symbol: Optional[str] = None,
    category: str = "stocks",
    limit: int = 10,
) -> list[dict]:
    """
    Fetch financial news from RSS feeds.

    Args:
        symbol:   Optional ticker filter. If provided, only returns headlines
                  that mention the symbol (case-insensitive). For category
                  ``pl_stocks`` the ticker is also expanded to company-name
                  aliases (e.g. ``KGH`` matches "KGHM" and "Polska Miedź").
        category: Feed group — "crypto" | "stocks" | "pl_stocks" | "all"
        limit:    Maximum number of items to return

    Returns:
        List of news items with title, url, published, summary, source.
    """
    if not _FEEDPARSER_AVAILABLE:
        return [{
            "error": "feedparser not installed. Run: pip install feedparser",
            "install": "pip install feedparser"
        }]

    feeds = RSS_FEEDS.get(category, RSS_FEEDS["stocks"])
    search_terms = _symbol_search_terms(symbol, category) if symbol else []
    results: list[dict] = []
    seen_titles: set[tuple[str, str]] = set()

    def _try_add(item: dict) -> None:
        """Append *item* unless it's a (title, source) duplicate or stale."""
        key = (item["title"].strip().lower(), item["source"])
        if not item["title"] or key in seen_titles:
            return
        if _is_stale(item["published"]):
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
            feed = feedparser.parse(feed_info["url"])
            source_name = feed.feed.get("title", feed_info["name"])

            for entry in feed.entries:
                if len(results) >= fetch_target:
                    break

                title = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")

                if search_terms:
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

        except Exception:
            continue

    # PAP Biznes has no RSS — synthesise feed from HTML scraper for pl_stocks.
    if category == "pl_stocks" and len(results) < fetch_target:
        try:
            from tradingview_mcp.core.services.pap_scraper import fetch_pap_items
            for item in fetch_pap_items(limit=15):
                if len(results) >= fetch_target:
                    break
                if search_terms:
                    haystack = f"{item['title']} {item['url']}".upper()
                    if not any(term in haystack for term in search_terms):
                        continue
                _try_add(item)
        except Exception:
            pass

    # Phase 2: sort by freshness (newest first), then trim to *limit*.
    results.sort(key=lambda i: _parse_published(i["published"]) or _EPOCH, reverse=True)
    return results[:limit]


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
    """
    items = fetch_news(symbol, category, limit)
    return {
        "symbol": symbol,
        "category": category,
        "count": len(items),
        "feedparser_available": _FEEDPARSER_AVAILABLE,
        "items": items,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ─── Utils ────────────────────────────────────────────────────────────────────

def _clean_html(text: str) -> str:
    """Strip basic HTML tags from text."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    return text.strip()
