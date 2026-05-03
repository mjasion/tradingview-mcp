"""BETA ETF NAV scraper — fetches certificate valuation from agiofunds.pl.

Why this exists: BETA ETFs trade on GPW with a market price (visible via
TradingView/Stooq) but, as closed-end funds, their fair value is the
*Wycena Certyfikatu* (NAV per certificate). Comparing market vs. NAV
exposes premium/discount — the only signal that actually matters for a
PFIZ. The market price alone tells you nothing.

Data lives behind a JS confirmation gate at
``agiofunds.pl/fundusz/{slug}/?confirm=true``. The gate is purely
client-side; the query param is enough for HTTP fetch — no cookie needed.

Each fund page exposes:
  * Ticker / ISIN
  * SWAN — Skorygowana Wartość Aktywów Netto (total assets in PLN)
  * History table of (date, NAV per certificate, certificates outstanding)

We extract the *latest* row and SWAN. Output cached for 6h — NAV updates
once per session day, page is heavy (~1MB).

If the slug map gets out of date (BETA adds a new fund), pull the
authoritative list from ``betaetf.pl/nasze-fundusze/_payload.json``.
"""
from __future__ import annotations

import re
import urllib.request
from typing import Optional

from tradingview_mcp.core.services.cache import cached
from tradingview_mcp.core.services.log import get_logger
from tradingview_mcp.core.services.proxy_manager import build_opener_with_proxy

_log = get_logger("beta_etf")

_BASE = "https://agiofunds.pl/fundusz"
_UA = "Mozilla/5.0 (compatible; tradingview-mcp/0.7.1; +beta-etf-scraper)"
_TIMEOUT = 10

# GPW ticker → agiofunds.pl URL slug. Verified 2026-05-03.
# Source of truth for new tickers: betaetf.pl/nasze-fundusze/_payload.json
_TICKER_TO_SLUG: dict[str, str] = {
    "ETFBW20TR":  "beta-etf-wig20tr-pfiz",
    "ETFBW20LV":  "beta-etf-wig20lev",
    "ETFBW20ST":  "beta-etf-wig20short",
    "ETFBM40TR":  "beta-etf-mwig40tr",
    "ETFBM40LV":  "beta-etf-mwig40trlv-pfiz",
    "ETFBM40ST":  "beta-etf-mwig40trsh-pfiz",
    "ETFBS80TR":  "beta-etf-swig80tr",
    "ETFBSPXPL":  "beta-etf-sp-500-pln-hedged",
    "ETFBNDXPL":  "beta-etf-nasdaq-100-pln-hedged",
    "ETFBNQ2ST":  "beta-etf-nasdaq-100-2xshort",
    "ETFBNQ3LV":  "beta-etf-nasdaq-100-3x-lev",
    "ETFBTBSP":   "beta-etf-tbsp",
    "ETFBDIVPL":  "beta-etf-dywidenda-plus-portfelowy-fiz",
    "ETFBTCPL":   "beta-etf-bitcoin-portfelowy-fiz",
}

# Latest row of the certificates history table — first <tr class="h-24">
# inside the wycena <tbody>. Columns: date, NAV per certificate, count.
_LATEST_ROW_RE = re.compile(
    r'<tr class="h-24">\s*'
    r'<td[^>]*>(\d{4}-\d{2}-\d{2})</td>\s*'
    r'<td[^>]*>([\d.,]+)</td>\s*'
    r'<td[^>]*>([\d\s,.]+)</td>',
    re.IGNORECASE,
)
_TICKER_RE = re.compile(
    r'Ticker</span></strong></th>\s*<td[^>]*>([A-Z0-9]+)',
    re.IGNORECASE,
)
_ISIN_RE = re.compile(
    r'ISIN</span></strong></th>\s*<td[^>]*>([A-Z0-9]+)',
    re.IGNORECASE,
)
# SWAN appears inside a sibling <td> as "300 435 898,00 zł". The label
# itself contains "(SWAN)" but the surrounding markup varies — we anchor
# on the bare ")SWAN)" suffix or on "Aktywów Netto … zł" pattern.
_SWAN_RE = re.compile(
    r'SWAN[^<]*</th>\s*<td[^>]*>([\d\s,.]+)\s*z[lł]',
    re.IGNORECASE,
)


def _parse_pl_number(s: str) -> Optional[float]:
    """'4 278 760,00' → 4278760.0   |   '69.74' → 69.74   |  bad → None."""
    s = s.strip().replace("\xa0", " ").replace(" ", "")
    # If the string contains both '.' and ',', the comma is the decimal sep.
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _fetch(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept-Language": "pl-PL,pl;q=0.9"}
    )
    opener = build_opener_with_proxy(_UA)
    with opener.open(req, timeout=_TIMEOUT) as r:
        return r.read(2_000_000).decode("utf-8", errors="replace")


def _parse_etf_page(html: str) -> dict:
    """Extract NAV / SWAN / metadata from one fund page. Empty dict on failure."""
    out: dict = {}
    if m := _TICKER_RE.search(html):
        out["ticker"] = m.group(1)
    if m := _ISIN_RE.search(html):
        out["isin"] = m.group(1)
    if m := _LATEST_ROW_RE.search(html):
        out["nav_date"] = m.group(1)
        nav = _parse_pl_number(m.group(2))
        certs = _parse_pl_number(m.group(3))
        if nav is not None:
            out["nav"] = round(nav, 4)
        if certs is not None:
            out["certificates_outstanding"] = certs
    if m := _SWAN_RE.search(html):
        swan = _parse_pl_number(m.group(1))
        if swan is not None:
            out["assets_pln"] = swan
    return out


@cached(ttl_seconds=6 * 3600, namespace="beta_etf_nav")
def get_etf_nav(ticker: str) -> dict:
    """Return BETA ETF NAV snapshot for a GPW ticker.

    Output shape::

        {"ticker": "ETFBW20TR", "isin": "PLBTETF00015",
         "nav": 69.74, "nav_date": "2026-04-29",
         "certificates_outstanding": 4278760.0,
         "assets_pln": 300435898.0,
         "source": "agiofunds.pl"}

    On unknown ticker / parse failure, returns ``{"error": ...}`` — never
    raises. ``source`` is always set so the caller can attribute the data.
    """
    sym = ticker.strip().upper()
    slug = _TICKER_TO_SLUG.get(sym)
    if not slug:
        return {
            "ticker": sym,
            "error": (
                f"unknown BETA ETF ticker '{sym}' — known tickers: "
                + ", ".join(sorted(_TICKER_TO_SLUG))
            ),
            "source": "agiofunds.pl",
        }

    url = f"{_BASE}/{slug}/?confirm=true"
    _log.info("fetching BETA ETF NAV for %s (%s)", sym, slug)
    try:
        html = _fetch(url)
    except Exception as e:
        _log.warning("agiofunds.pl fetch failed for %s: %s", sym, e)
        return {"ticker": sym, "error": f"{type(e).__name__}: {e}", "source": "agiofunds.pl"}

    parsed = _parse_etf_page(html)
    if "nav" not in parsed:
        _log.warning("agiofunds.pl: NAV row not found for %s", sym)
        return {
            "ticker": sym,
            "error": "NAV row not found in agiofunds.pl page (layout may have changed)",
            "source": "agiofunds.pl",
        }

    parsed["ticker"] = parsed.get("ticker", sym)
    parsed["source"] = "agiofunds.pl"
    parsed["url"] = url
    _log.info("BETA ETF %s: NAV %s zł on %s (assets %.0f PLN)",
              sym, parsed["nav"], parsed.get("nav_date"),
              parsed.get("assets_pln") or 0)
    return parsed


def supported_tickers() -> list[str]:
    """Return list of BETA ETF tickers supported by ``get_etf_nav``."""
    return sorted(_TICKER_TO_SLUG.keys())
