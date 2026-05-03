"""PAP Biznes scraper — synthesizes RSS-shaped news from biznes.pap.pl HTML.

Why: PAP Biznes does not expose a public RSS feed (verified — all known
endpoints return 0 entries). The HTML site is fully crawlable and uses
predictable URLs:  /wiadomosci/{kategoria}/{slug-of-the-headline}

We fetch the listing page once and turn each article link into an
RSS-shaped item:  {title, url, published, summary, source}.

Design choices:
  * No per-article fetch for the listing — derive the title from the slug.
    Polish slugs are dash-separated lowercased headlines; common tokens
    (śląsk, łódź, będzie, …) get their diacritics restored via
    ``polish_diacritics.SLUG_DIACRITICS``. Unknown tokens stay as-is.
  * Per-article fetch IS done for *published date* because biznes.pap.pl
    article pages display ``Publikacja: YYYY-MM-DD HH:MM``. This costs
    one extra HTTP request per item, so we only enrich the top *limit*
    items (default 10) and fail-soft to ``""`` on any error.
  * Single network request for the listing; up to *limit* requests for
    per-article dates.

If PAP ever publishes an RSS feed, drop this module and add the URL to
news_service.RSS_FEEDS["pl_stocks"].
"""
from __future__ import annotations

import re
import urllib.request
from typing import Iterable

from tradingview_mcp.core.services.log import get_logger

_log = get_logger("pap")

from tradingview_mcp.core.data.polish_diacritics import restore_diacritics
from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy

_LISTING_URL = "https://biznes.pap.pl/"
_BASE = "https://biznes.pap.pl"
_UA = "Mozilla/5.0 (compatible; tradingview-mcp/0.7.1; +pap-scraper)"
_TIMEOUT = 8
_ARTICLE_TIMEOUT = 5

# Article URLs look like /wiadomosci/{firmy|gospodarka|przemysl|rynki|...}/{slug}
# Slugs are 20+ chars, all-lowercase ASCII (Polish diacritics stripped by PAP).
_ARTICLE_RE = re.compile(
    r'href="(/wiadomosci/[a-z0-9-]+/[a-z0-9-]{15,})"',
    re.I,
)

# Article HTML contains:  <strong>Publikacja: </strong>2026-05-03 15:20
# Tolerate any HTML tags / whitespace between the label and the date.
_PUBLISHED_RE = re.compile(
    r"Publikacja:\s*(?:</?[a-z][^>]*>\s*)*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})",
    re.I,
)


def _fetch(url: str, timeout: int = _TIMEOUT) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept-Language": "pl-PL,pl;q=0.9"}
    )
    try:
        opener = build_opener_with_proxy(_UA)
        with opener.open(req, timeout=timeout) as r:
            return r.read(500_000).decode("utf-8", errors="replace")
    except Exception:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(500_000).decode("utf-8", errors="replace")


def _slug_to_title(path: str) -> str:
    """/wiadomosci/firmy/jsw-bedzie-rosnac-na-slasku -> 'Jsw będzie rosnąć na śląsku'."""
    slug = path.rsplit("/", 1)[-1]
    if not slug:
        return ""
    tokens = [restore_diacritics(t) for t in slug.split("-") if t]
    if not tokens:
        return ""
    tokens[0] = tokens[0][:1].upper() + tokens[0][1:]
    return " ".join(tokens)


def _fetch_published(url: str) -> str:
    """Return ISO-8601 ``published`` extracted from article page, or ``""``.

    PAP timestamps are local Warsaw time (no offset displayed). We emit
    ``YYYY-MM-DDTHH:MM:00+02:00`` — the offset is approximate (Warsaw is
    +02:00 in summer, +01:00 in winter); we use +02:00 unconditionally
    because portfolio-grade news ranking only cares about ordering, not
    sub-hour precision.
    """
    try:
        html = _fetch(url, timeout=_ARTICLE_TIMEOUT)
    except Exception:
        return ""
    m = _PUBLISHED_RE.search(html)
    if not m:
        return ""
    raw = m.group(1)  # "2026-05-03 15:20"
    return raw.replace(" ", "T") + ":00+02:00"


def fetch_pap_items(limit: int = 30, with_dates: bool = True) -> list[dict]:
    """Return up to *limit* recent items from biznes.pap.pl listing.

    Each item shape matches news_service feed-item shape:
        {title, url, published, summary, source}

    With *with_dates=True* (default), each returned item gets a per-article
    HTTP fetch to populate ``published``. Cost: up to *limit* requests
    after the listing fetch. Set *with_dates=False* to skip (e.g. tests).
    """
    _log.info("scraping PAP Biznes headlines (limit=%d)", limit)
    try:
        html = _fetch(_LISTING_URL)
    except Exception as e:
        _log.warning("PAP listing fetch failed: %s", e)
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

    if with_dates:
        _log.debug("fetching publication dates for %d PAP articles", len(out))
        for item in out:
            item["published"] = _fetch_published(item["url"])

    _log.info("PAP: collected %d articles", len(out))
    return out


def _iter_article_paths(html: str) -> Iterable[str]:
    for m in _ARTICLE_RE.finditer(html):
        yield m.group(1)
