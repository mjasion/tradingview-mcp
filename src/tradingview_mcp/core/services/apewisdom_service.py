"""ApeWisdom — Reddit mention volume, used when the Reddit JSON API is blocked.

Why this exists: ``sentiment_service`` reads ``reddit.com/r/<sub>/search.json``
directly, and Reddit now answers **403** to server traffic (verified
2026-08-30: 403 direct, 403 via proxy on ``old.reddit.com``, timeout on
``www.reddit.com``). That left the sentiment tool returning a well-formed
"Neutral / 0 posts" response that was indistinguishable from a real neutral
reading. ApeWisdom aggregates the same subreddits hourly and serves a plain
JSON ranking that is reachable from a datacenter IP.

What it measures — and what it does not: ApeWisdom returns **how often** a
ticker is mentioned, not whether the mentions are bullish or bearish. So this
module never produces a ``sentiment_score``; callers get a mention block and
must label it as volume, not direction.

Shape of the upstream response (``/api/v1.0/filter/<filter>/page/<n>``)::

    {"count": 702, "pages": 8, "current_page": 1,
     "results": [{"rank": 1, "ticker": "SPY", "name": "SPDR S&P 500 ETF Trust",
                  "mentions": 60, "upvotes": 115,
                  "rank_24h_ago": 2, "mentions_24h_ago": 128}, ...]}

The endpoint is a *ranking*, not a per-ticker lookup, so a query walks pages
until the ticker shows up or ``max_pages`` is exhausted. Pages are ticker-
independent and cached for 30 minutes (upstream refreshes hourly), which keeps
a scan of several tickers down to one set of fetches.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from tradingview_mcp.core.services.cache import cached
from tradingview_mcp.core.services.log import get_logger
from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy

_log = get_logger("apewisdom")

_API = "https://apewisdom.io/api/v1.0/filter/{filter}/page/{page}"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_TIMEOUT = 12
_PAGE_TTL = 1800  # 30 min; upstream recomputes hourly

# sentiment_service categories → ApeWisdom filters
_FILTERS = {
    "stocks": "all-stocks",
    "crypto": "all-crypto",
    "all": "all",
}

#: Subreddits ApeWisdom aggregates for the stock filters, for provenance.
COVERED_SUBREDDITS = {
    "all-stocks": ["r/wallstreetbets", "r/stocks", "r/investing", "r/stockmarket",
                   "r/options", "r/pennystocks"],
    "all-crypto": ["r/CryptoCurrency", "r/CryptoMarkets", "r/SatoshiStreetBets",
                   "r/Bitcoin", "r/ethtrader"],
    "all": ["r/wallstreetbets", "r/stocks", "r/investing", "r/CryptoCurrency"],
}


def _filter_for(category: str) -> str:
    return _FILTERS.get((category or "all").strip().lower(), _FILTERS["all"])


@cached(ttl_seconds=_PAGE_TTL, namespace="apewisdom_page")
def _fetch_page(filter_name: str, page: int) -> dict:
    """Fetch one ranking page. Returns the raw payload or an ``error`` dict.

    Error dicts are never cached (see ``cache.cached``), so a transient outage
    doesn't suppress real data for half an hour.
    """
    url = _API.format(filter=filter_name, page=page)
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json",
    })
    try:
        opener = build_opener_with_proxy(_UA)
        with opener.open(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"apewisdom_http_{e.code}"}
    except urllib.error.URLError as e:
        return {"error": f"apewisdom_unreachable: {e.reason}"}
    except (ValueError, KeyError) as e:
        return {"error": f"apewisdom_parse_error: {type(e).__name__}"}
    except Exception as e:  # socket.timeout, ssl errors, …
        return {"error": f"apewisdom_error: {type(e).__name__}"}


def _pct_change(now: int, before) -> float | None:
    if before in (None, 0):
        return None
    return round((now - before) / before * 100, 1)


def fetch_mentions(symbol: str, category: str = "all", max_pages: int = 3) -> dict:
    """Look a ticker up in the ApeWisdom ranking.

    Args:
        symbol:    ticker to find (case-insensitive, ``BTCUSDT`` → ``BTC``).
        category:  ``"stocks"`` | ``"crypto"`` | ``"all"`` — maps to the
                   matching ApeWisdom filter.
        max_pages: how deep to walk the ranking (100 tickers per page).

    Returns one of three shapes, always with an explicit ``status``:

    * ``status="ok"`` — ticker found: rank, mentions, 24h deltas, upvotes.
    * ``status="not_ranked"`` — upstream reachable, ticker below the scanned
      window. That is a real reading ("no measurable retail chatter"), not a
      failure, so ``mentions_24h`` is 0 and ``scanned_top`` says how deep we
      looked.
    * ``status="unavailable"`` — upstream failed; ``error`` says how.
    """
    ticker = _normalize(symbol)
    filter_name = _filter_for(category)
    scanned = 0
    total = None

    for page in range(1, max_pages + 1):
        payload = _fetch_page(filter_name, page)
        if "error" in payload:
            _log.warning("ApeWisdom %s page %d failed: %s", filter_name, page, payload["error"])
            return {
                "status": "unavailable",
                "source": "apewisdom",
                "ticker": ticker,
                "error": payload["error"],
                "recommended_action": (
                    "Reddit mention volume could not be read. Treat the sentiment "
                    "pillar as MISSING — do not record it as neutral."
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        results = payload.get("results") or []
        total = payload.get("count", total)
        scanned += len(results)
        for row in results:
            if str(row.get("ticker", "")).upper() != ticker:
                continue
            mentions = int(row.get("mentions") or 0)
            prev = row.get("mentions_24h_ago")
            _log.info("ApeWisdom %s: rank %s, %d mentions (24h)", ticker, row.get("rank"), mentions)
            return {
                "status": "ok",
                "source": "apewisdom",
                "ticker": ticker,
                "name": row.get("name"),
                "rank": row.get("rank"),
                "rank_24h_ago": row.get("rank_24h_ago"),
                "mentions_24h": mentions,
                "mentions_prev_24h": prev,
                "mentions_change_pct": _pct_change(mentions, prev),
                "upvotes": row.get("upvotes"),
                "universe_size": total,
                "subreddits": COVERED_SUBREDDITS.get(filter_name, []),
                "measures": "reddit mention volume — not directional sentiment",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        if payload.get("pages") and page >= int(payload["pages"]):
            break

    _log.info("ApeWisdom %s: not in the top %d of %s", ticker, scanned, filter_name)
    return {
        "status": "not_ranked",
        "source": "apewisdom",
        "ticker": ticker,
        "mentions_24h": 0,
        "scanned_top": scanned,
        "universe_size": total,
        "subreddits": COVERED_SUBREDDITS.get(filter_name, []),
        "measures": "reddit mention volume — not directional sentiment",
        "note": (
            f"Ticker absent from the top {scanned} most-mentioned tickers. "
            f"Upstream answered, so this is a reading (no measurable retail "
            f"chatter), not a data gap."
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _normalize(symbol: str) -> str:
    """``btcusdt`` → ``BTC``, ``AAPL`` → ``AAPL``. ApeWisdom indexes bare tickers."""
    s = (symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    for quote in ("USDT", "USDC", "USD", "PLN", "EUR"):
        if len(s) > len(quote) + 1 and s.endswith(quote):
            return s[: -len(quote)]
    return s
