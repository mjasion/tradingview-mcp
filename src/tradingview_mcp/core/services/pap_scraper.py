"""PAP Biznes scraper — synthesizes RSS-shaped news from biznes.pap.pl HTML.

Why: PAP Biznes does not expose a public RSS feed (verified — all known
endpoints return 0 entries). The HTML site is fully crawlable and uses
predictable URLs:  /wiadomosci/{kategoria}/{slug-of-the-headline}

We fetch the listing page once and turn each article link into an
RSS-shaped item:  {title, url, published, summary, source}.

Design choices:
  * No per-article fetch — derive the title from the slug. Polish slugs
    are dash-separated lowercased headlines, so "jsw-widzi-ryzyko-..."
    becomes "Jsw widzi ryzyko ...". Loses Polish diacritics but is
    enough for ticker / company-name filtering (see gpw_company_names).
  * Single network request per call. Listing already contains 30-60
    fresh items, more than enough for the 10-item news tool.
  * No date — biznes.pap.pl does not surface publication time on the
    listing or in article meta tags. ``published`` is left empty;
    consumers treat it as unknown.

If PAP ever publishes an RSS feed, drop this module and add the URL to
news_service.RSS_FEEDS["pl_stocks"].
"""
from __future__ import annotations

import re
import urllib.request
from typing import Iterable

from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy

_LISTING_URL = "https://biznes.pap.pl/"
_BASE = "https://biznes.pap.pl"
_UA = "Mozilla/5.0 (compatible; tradingview-mcp/0.7.1; +pap-scraper)"
_TIMEOUT = 8

# Article URLs look like /wiadomosci/{firmy|gospodarka|przemysl|rynki|...}/{slug}
# Slugs are 20+ chars, all-lowercase ASCII (Polish diacritics stripped by PAP).
_ARTICLE_RE = re.compile(
    r'href="(/wiadomosci/[a-z0-9-]+/[a-z0-9-]{15,})"',
    re.I,
)


def _fetch(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept-Language": "pl-PL,pl;q=0.9"}
    )
    try:
        opener = build_opener_with_proxy(_UA)
        with opener.open(req, timeout=_TIMEOUT) as r:
            return r.read(500_000).decode("utf-8", errors="replace")
    except Exception:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read(500_000).decode("utf-8", errors="replace")


def _slug_to_title(path: str) -> str:
    """/wiadomosci/firmy/jsw-widzi-ryzyko-... -> 'Jsw widzi ryzyko ...'."""
    slug = path.rsplit("/", 1)[-1]
    words = slug.replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else ""


def fetch_pap_items(limit: int = 30) -> list[dict]:
    """Return up to *limit* recent items from biznes.pap.pl listing.

    Each item shape matches news_service feed-item shape:
        {title, url, published, summary, source}
    """
    try:
        html = _fetch(_LISTING_URL)
    except Exception:
        return []

    seen: set[str] = set()
    out: list[dict] = []
    for path in _iter_article_paths(html):
        if path in seen:
            continue
        seen.add(path)
        out.append({
            "title": _slug_to_title(path),
            "url": _BASE + path,
            "published": "",
            "summary": "",
            "source": "PAP Biznes",
        })
        if len(out) >= limit:
            break
    return out


def _iter_article_paths(html: str) -> Iterable[str]:
    for m in _ARTICLE_RE.finditer(html):
        yield m.group(1)
