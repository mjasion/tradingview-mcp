"""SEC EDGAR — Form 4 (insider transactions) + recent filings.

Why this exists: insider activity is a real signal for US single-name positions
("director just dumped 50k shares", "CEO bought 100k after the dip"). The MCP
already wraps Yahoo, TradingView and Reddit; SEC EDGAR closes the loop on US
fundamentals without adding a paid data vendor.

Strategy:
1. Map ticker → CIK using ``/files/company_tickers.json`` (cached 7 days,
   ~800KB, ~10k US issuers).
2. Fetch ``/submissions/CIK{cik:0>10}.json`` and filter ``form == "4"`` from
   the parallel arrays under ``filings.recent``.
3. Build viewer URLs for each filing — Claude can follow them when the user
   wants the transaction breakdown.

Limitations:
* Only US-listed issuers covered by EDGAR. GPW / WSE has no equivalent.
* We do NOT parse ``form4.xml`` for transaction codes / amounts — too brittle
  for an MVP. The filing URL gives Claude a follow-up path.
* SEC EDGAR is sandbox-blocked in some environments; the tool returns an
  ``error`` dict on network failure, never raises.

Fair-access policy: SEC requires a User-Agent identifying the requester. We
send ``tradingview-mcp/<version> contact: <email>``.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from tradingview_mcp.core.services.cache import cached

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
_FILING_VIEWER_BASE = "https://www.sec.gov/Archives/edgar/data"
_UA = "tradingview-mcp/0.5 contact: marcinjasion@gmail.com"
_TIMEOUT = 12


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


@cached(ttl_seconds=604800, namespace="sec_ticker_map")  # 7 days
def _load_ticker_map() -> dict[str, dict]:
    """Build lowercase-ticker → ``{cik, name}`` map. Cached for a week."""
    raw = _http_json(_TICKERS_URL)
    out: dict[str, dict] = {}
    for entry in raw.values():
        t = entry.get("ticker")
        cik = entry.get("cik_str")
        name = entry.get("title")
        if not t or cik is None:
            continue
        out[t.upper()] = {"cik": int(cik), "name": name}
    return out


def lookup_cik(symbol: str) -> Optional[dict]:
    """Resolve ``symbol`` to ``{cik, name}`` or None."""
    try:
        table = _load_ticker_map()
    except Exception:
        return None
    return table.get(symbol.upper())


def _filing_url(cik: int, accession: str, primary_doc: str) -> str:
    """Build the filing landing URL on EDGAR."""
    acc_no_dashes = accession.replace("-", "")
    return f"{_FILING_VIEWER_BASE}/{cik}/{acc_no_dashes}/{primary_doc}"


@cached(ttl_seconds=3600, namespace="sec_insider")  # 1h
def get_insider_transactions(symbol: str, limit: int = 10) -> dict:
    """Recent Form 4 (insider) filings for *symbol*. Never raises.

    Returns a dict shaped like::

      {
        "symbol": "AAPL", "cik": 320193, "name": "Apple Inc.",
        "filings": [
          {"date": "2026-04-27", "accession": "...", "url": "https://..."},
          ...
        ],
        "count": <total Form-4 filings in the recent window>,
        "source": "SEC EDGAR",
        "timestamp": "...",
      }

    On lookup failure (unknown ticker, network error) returns an ``error`` field.
    """
    out: dict = {"symbol": symbol.upper(), "source": "SEC EDGAR"}
    info = lookup_cik(symbol)
    if not info:
        return {**out, "error": f"ticker {symbol.upper()} not found in SEC EDGAR"}

    cik = info["cik"]
    out["cik"] = cik
    out["name"] = info["name"]

    url = f"{_SUBMISSIONS_BASE}/CIK{cik:010d}.json"
    try:
        body = _http_json(url)
    except Exception as e:
        return {**out, "error": f"{type(e).__name__}: {e}"}

    recent = body.get("filings", {}).get("recent", {}) or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    primaries = recent.get("primaryDocument") or []

    filings: list[dict] = []
    for i, form in enumerate(forms):
        if form != "4":
            continue
        if i >= len(dates) or i >= len(accs) or i >= len(primaries):
            break
        filings.append({
            "date": dates[i],
            "accession": accs[i],
            "url": _filing_url(cik, accs[i], primaries[i]),
        })
        if len(filings) >= limit:
            break

    return {
        **out,
        "filings": filings,
        "count": sum(1 for f in forms if f == "4"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
