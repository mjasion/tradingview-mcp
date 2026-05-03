"""Generate ticker lists for new exchanges using TradingView Screener.

Run from repo root after dependencies are installed:

    python scripts/generate_coinlists.py

Writes one symbol per line to src/tradingview_mcp/coinlist/{exchange}.txt.
Symbols are sorted by market cap descending. Existing files are overwritten.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from tradingview_screener import Query

REPO_ROOT = Path(__file__).resolve().parent.parent
COINLIST_DIR = REPO_ROOT / "src" / "tradingview_mcp" / "coinlist"

# (filename, screener-market, limit)
MARKETS: list[tuple[str, str, int]] = [
    ("gpw",      "poland",      400),
    ("xetra",    "germany",     400),
    ("lse",      "uk",          500),
    ("tsx",      "canada",      400),
    ("euronext", "france",      300),
    ("ams",      "netherlands", 200),
    ("ebr",      "belgium",     100),
    ("mil",      "italy",       250),
    ("bme",      "spain",       150),
    ("six",      "switzerland", 200),
    ("vie",      "austria",     100),
    ("osl",      "norway",      200),
    ("omxsto",   "sweden",      300),
    ("omxcop",   "denmark",     150),
    ("omxhex",   "finland",     150),
    ("tse",      "japan",       500),
    ("krx",      "korea",       400),
]

# Symbols always present even if not in top-N by market cap (e.g. user portfolio)
PINNED: dict[str, list[str]] = {
    "gpw": ["KGH", "CDR", "JSW", "CRI", "CRQ", "ETFBW20TR", "ETFBCASH", "ETFBS80TR"],
    "xetra": ["SAP"],
}


def fetch_symbols(market: str, limit: int) -> list[str]:
    _, df = (
        Query()
        .set_markets(market)
        .select("name", "market_cap_basic")
        .order_by("market_cap_basic", ascending=False)
        .limit(limit)
        .get_scanner_data()
    )
    return [s for s in df["name"].tolist() if isinstance(s, str) and s]


def main() -> int:
    COINLIST_DIR.mkdir(parents=True, exist_ok=True)
    for fname, market, limit in MARKETS:
        try:
            symbols = fetch_symbols(market, limit)
        except Exception as e:
            print(f"FAIL {fname} ({market}): {e}", file=sys.stderr)
            continue

        for pin in PINNED.get(fname, []):
            if pin not in symbols:
                symbols.insert(0, pin)

        seen: set[str] = set()
        unique: list[str] = []
        for s in symbols:
            if s not in seen:
                unique.append(s)
                seen.add(s)

        out = COINLIST_DIR / f"{fname}.txt"
        out.write_text("\n".join(unique) + "\n", encoding="utf-8")
        print(f"OK   {fname}.txt  {len(unique)} symbols")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
