# CLAUDE.md

Guide for Claude Code working in this repository.

## What this is

MCP server exposing ~29 tools for technical analysis, screening, backtesting,
sentiment, and news — wrapping TradingView, Yahoo Finance, Stooq, and Reddit.
Fork of `atilaahmettaner/tradingview-mcp` extended with global stock market
coverage and Polish data providers.

Entry point: `src/tradingview_mcp/server.py` (`tradingview-mcp` script).
Transport: `streamable-http` on port 8000 by default; also `stdio` for desktop
clients.

## Layout

```
src/tradingview_mcp/
├── server.py                          # all @mcp.tool() definitions live here
├── coinlist/                          # per-exchange ticker universes (.txt, one symbol per line)
└── core/
    ├── services/                      # one module per data source / capability
    │   ├── yahoo_finance_service.py   # spot prices (US + global)
    │   ├── stooq_service.py           # GPW spot prices (Yahoo doesn't cover .WA)
    │   ├── pap_scraper.py             # PAP Biznes news (HTML scraper, no public RSS)
    │   ├── news_service.py            # RSS aggregator + PAP fallback
    │   ├── sentiment_service.py       # Reddit sentiment
    │   ├── screener_service.py        # TradingView screener wrappers
    │   ├── scanner_service.py         # signal-based scanners
    │   ├── backtest_service.py        # 6 strategy backtests
    │   ├── bitcoin_market_service.py
    │   ├── multi_agent_service.py     # combined_analysis orchestration
    │   ├── indicators.py              # indicator calculations
    │   ├── egx_service.py             # Egypt-specific tools
    │   ├── proxy_manager.py           # rotating proxies for blocked sources
    │   ├── coinlist.py                # loads coinlist/*.txt
    │   └── screener_provider.py
    ├── utils/validators.py            # exchange/symbol normalisation, alias maps
    ├── data/
    │   ├── gpw_company_names.py       # GPW ticker → search aliases (KGH → KGHM, Polska Miedź)
    │   ├── egx_indices.py
    │   └── egx_sectors.py
    └── portfolio.py                   # portfolio helpers
scripts/generate_coinlists.py          # regenerates coinlist/*.txt from TV Screener API
tests/unit/                            # pytest, all fast (no network)
```

## Conventions

- **All MCP tools live in `server.py`**. Keep them thin: parse args, call a
  service, return a dict. Business logic belongs in `core/services/`.
- **Exchange routing** goes through `validators.py`:
  - `EXCHANGE_SCREENER` — alias → tradingview-screener market name
  - `_EXCHANGE_TV_PREFIX` — alias → TradingView symbol prefix (e.g. `gpw` → `GPW`)
  - `STOCK_EXCHANGES` — set of stock-exchange aliases
  - `sanitize_exchange()` — normalise input, default to crypto if unknown
  - `get_tv_exchange_prefix()` — get the TradingView prefix for `EXCH:TICKER`
- **Adding a new exchange**: update those three structures + add a
  `coinlist/<alias>.txt` (run `scripts/generate_coinlists.py` to populate).
- **Service modules** are independent and side-effect-free except for HTTP.
  Each service returns plain dicts shaped like Yahoo Finance quote dicts where
  applicable so they're swappable.

## Polish-market specifics

- `yahoo_price()` routes Polish tickers to Stooq:
  - `.WA` suffix → always Stooq
  - Symbol in `_STOOQ_PRIMARY_GPW_TICKERS` (curated WIG20 + portfolio) → Stooq
  - Everything else (incl. AAPL CFDs listed on GPW) → Yahoo
- `combined_analysis(exchange="gpw")` skips Reddit (low PL coverage),
  flags `confidence: TECHNICAL_ONLY`, surfaces Polish RSS instead.
- News for `category="pl_stocks"`: 4 RSS feeds + PAP Biznes via
  `pap_scraper.py` (HTML; PAP has no public RSS — verified all known endpoints).
- Symbol filter on Polish news expands the ticker via
  `gpw_company_names.GPW_COMPANY_NAMES` (e.g. `KGH` matches "KGHM", "Polska Miedź").

## Running locally

```bash
uv run tradingview-mcp                    # streamable-http on :8000
uv run tradingview-mcp stdio              # for Claude Desktop / similar
uv run pytest -q                          # 116 tests, ~50ms, no network
docker compose up -d                      # full stack with autoheal sidecar
```

Sandbox: `stooq.com` is on the allowed-host list. Other external HTTP
hosts may need `dangerouslyDisableSandbox: true` for ad-hoc probes.

## Tests

- All fast, no network. If a test requires HTTP, it's wrong — mock or move to
  an integration tier (none yet).
- `tests/unit/test_exchange_aliases.py` covers the alias maps; add
  parametrized rows there when registering a new exchange.
- Run `uv run pytest -q` before any commit. Suite must stay green.

## Commits

- Conventional commits: `feat(scope):`, `fix(scope):`, `chore:`.
- No emojis in subject line; emojis OK in body.
- Don't bump the upstream PyPI package — the fork installs from git.
- LICENSE attribution to original author **must** stay (MIT requirement).
  Add `Copyright (c) <year> <name> (modifications)` for new contributors,
  don't remove existing notices.

## Things to avoid

- Don't add Reddit/English sentiment for GPW — stay with the Polish RSS path.
- Don't widen `_STOOQ_PRIMARY_GPW_TICKERS` to the full GPW coinlist; that
  re-introduces the AAPL-routes-to-Stooq bug (GPW lists US CFDs).
- Don't fetch per-article HTML in `pap_scraper.py` for the title; slug-derived
  titles are intentional (1 request vs N+1, fast enough for filtering).
- Don't hard-code `python3.X` paths in the Dockerfile — use `/usr/local/lib`
  so base-image bumps are version-agnostic.
