# CLAUDE.md

Guide for Claude Code working in this repository.

## What this is

MCP server exposing ~29 tools for technical analysis, screening, backtesting,
sentiment, and news — wrapping TradingView, Yahoo Finance, Stooq, RSS/Google News, and
Reddit (with an ApeWisdom fallback).
Fork of `atilaahmettaner/tradingview-mcp` extended with global stock market
coverage and Polish data providers.

Entry point: `src/tradingview_mcp/server.py` (`tradingview-mcp` script).
Transport: `stdio` when invoked bare (what desktop clients use); the Docker
image runs `streamable-http` on port 8000.

Built on the MCP Python SDK **2.x** (`mcp.server.mcpserver.MCPServer`; the v1
`mcp.server.fastmcp.FastMCP` import no longer exists). Host/port go to
`mcp.run(transport="streamable-http", host=..., port=...)`, not `mcp.settings`.
The SDK serves MCP only at `/mcp`; `/health` is our own `@mcp.custom_route`
in `server.py` — the Docker `HEALTHCHECK` and the k3s probes depend on it.
Binding to `127.0.0.1`/`localhost`/`::1` makes the SDK enforce DNS-rebinding
protection (mismatched `Host` header → 421); `0.0.0.0` leaves it off.

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
    │   ├── sentiment_service.py       # Reddit sentiment (+ ApeWisdom fallback)
    │   ├── apewisdom_service.py       # Reddit mention *volume* when Reddit itself 403s
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

## Data-quality contract

An upstream that fails must never be indistinguishable from a real reading.
The bug this rule exists for: Reddit started answering 403 to this server,
`_fetch_reddit_posts` swallowed it into `[]`, `analyze_sentiment` scored the
empty list as `0.0` / `"Neutral"`, and `combined_analysis` then found that
fabricated zero "agreed" with a bearish technical and reported
`confidence: HIGH`. A consumer had no way to tell.

Rules for any tool that reads an external source:

- **Every response carries a `status`.** A numeric reading is `None` when
  there is no sample — never `0.0`, never a neutral label.
- **Fetch helpers raise, callers classify.** `_fetch_reddit_posts` raises
  `RedditFetchError(reason)`; `news_service._parse_feed` raises. Error tags are
  short and machine-readable: `http_403`, `unreachable: <reason>`, `timeout`,
  `parse_error: <Type>`.
- **Name the casualties.** Multi-source tools report `sources_ok` /
  `sources_failed` (sentiment) or `sources: {attempted, ok, failed}` (news), so
  a partial answer says what is missing from it.
- **"Nothing found" is a reading; say so.** `no_data` / `no_matches` /
  `not_ranked` are distinct from `unavailable`, and carry a note spelling out
  that the zero is real.

`market_sentiment` statuses: `ok` · `partial` (some subreddits died, score
still computed) · `degraded` (Reddit unusable, ApeWisdom answered —
`sentiment_label: "MENTIONS_ONLY"`, score `None`, `mentions` block) ·
`no_data` (Reddit reachable, zero posts) · `unavailable` (everything failed).

`financial_news` statuses: `ok` · `partial` · `no_matches` (feeds alive, the
symbol just isn't in the news) · `no_news` · `unavailable` (every feed failed —
also sets `error: "all_feeds_failed"`).

`combined_analysis` only scores confluence when sentiment `status` is `ok` or
`partial`. Otherwise `signals_agree: None`, `confidence: "TECHNICAL_ONLY"`, and
the recommendation states why. `portfolio_scan` was already honest
(`upstream_warning`, `degraded_count`, `yahoo_rate_limit`) and additionally
surfaces `news_error`.

TradingView TA: an error envelope carries `upstream_status: "rate_limited"`
(plus `upstream_reason` `http_429`/`circuit_open`, `retry_after_seconds`) or
`"down"`. A stale-cache answer is `degraded: True`, `data_source:
"tradingview_cache"`, `cache_age_seconds`, and `coin_analysis` reports
`timestamp: "cached"` instead of `"real-time"`. `portfolio_scan` adds
`tradingview_rate_limit` and `likely_cause: "tradingview_rate_limited"`.

Tests that pin this contract: `test_sentiment_gaps.py`,
`test_news_health.py`, `test_apewisdom.py`, `test_combined_confluence.py`,
`test_tv_rate_limit.py`.
Adding a new source means adding its failure row there too.

## Sentiment and news sources

- **Reddit refuses unauthenticated `.json`** — OAuth-only policy since 2023,
  not a UA or IP filter: `www.reddit.com/r/*/search.json` answers 403
  (`server: snooserv`, no Cloudflare challenge) to a browser UA and a bot UA
  alike, and from a home IP as readily as from the proxies. `old.reddit.com`
  returns HTML instead of JSON, and `robots.txt` is `Disallow: /`. Verified
  2026-08-30. `POST /api/v1/access_token` answers **401, not 403**, from every
  egress — so a registered script app (client_credentials → `oauth.reddit.com`,
  100 QPM free) would restore directional sentiment. The owner has decided
  against registering one; don't re-litigate it, and don't try to route around
  the block with UA spoofing or proxy rotation (it fails anyway, and the
  Public Content Policy forbids it).
- **A failed sweep opens a circuit breaker** (`_BREAKER_TTL`, 6h, env
  `TRADINGVIEW_MCP_REDDIT_BREAKER_TTL`, `0` disables). Five dead subreddits
  cost ~34s — mostly 10s timeouts — on *every* `market_sentiment` and
  `combined_analysis`; with the breaker that is paid once per TTL and later
  calls are instant. A skipped sweep reports `sources_failed:
  {"reddit": "circuit_open: …"}` plus a `reddit_circuit` block, never fabricated
  403s. Any subreddit answering closes it, and the first call after the TTL
  probes again, so restored access self-heals. It is process-global, so
  `tests/unit/conftest.py` resets it around every test.
- **`apewisdom_service.py` is the fallback**: it aggregates the same subreddits
  and is reachable. It measures **mention volume, not direction** — never map
  its numbers onto a sentiment score. `fetch_mentions()` returns `ok` /
  `not_ranked` / `unavailable`; ranking pages are cached 30 min.
- **`feeds.reuters.com` is NXDOMAIN** and used to be the only `stocks` feed.
  Current list: MarketWatch Top Stories, CNBC Finance, CNBC Top News,
  Investing.com, Seeking Alpha. Verify a feed live before adding it.
- **Feeds go through `build_opener_with_proxy()`**, not `feedparser.parse(url)`
  — CNBC 403s feedparser's own UA, and the opener carries the egress proxy and
  the rate limiter. `feedparser` only parses bytes we hand it.
- **A per-symbol Google News search feed** is prepended whenever `symbol` is
  given; general feeds rarely name a mid-cap. Its results are `prefiltered`
  (the query already asked the ticker question), so the substring filter is
  skipped for it — "Apple beats on iPhone" is an AAPL story.

## TradingView scanner

- **`scanner.tradingview.com` rate-limits per egress IP**: CloudFront answers
  `429` with an *empty body* and no `Retry-After`. `tradingview_ta` ignores the
  status and `json.loads`es the body, so a 429 used to look exactly like a
  weekend blip (`JSONDecodeError`) and got retried 4× in 4s. Verified
  2026-10-17. Upstream (`atilaahmettaner/tradingview-mcp`) hasn't fixed it
  either — it only has a stale cache and a cooldown.
- **`tv_scanner.get_multiple_analysis` makes the request itself** (the library's
  payload via `TradingView.data`, the library's parser via `calculate`) so the
  status code is visible. Keep that name — tests monkeypatch it.
- **429 → a process-global gate**: 5s, doubling, cap 60s; every worker thread
  waits on it. A call waits and retries within a 60s budget; a call that runs
  out trips the breaker (gate shut 5 min, `circuit_open` fails fast, first call
  after the TTL probes, any success closes it). Env: `TRADINGVIEW_MCP_TV_429_*`,
  `_TV_BREAKER_TTL`, `_TV_STALE_TTL` — see the module docstring.
- **Stale cache** (≤6h, capped at 4 bars of the interval) is opt-in:
  `ta_call(..., allow_stale=True)`, and only for callers that then spread
  `stale_fields(analysis)` into their response. Error envelopes use
  `upstream_fields(exc)`.
- **TradingView traffic bypasses the egress proxies** — `requests.post`
  goes out directly, only the global rate limiter applies.
  `conftest.py` resets the gate around every test.

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
  **Alias order matters**: `aliases[0]` becomes the Google News PL query, so the
  first entry must be the name headlines actually print ("KGHM", not "KGH").

## Running locally

```bash
uv run tradingview-mcp                                 # stdio (the argparse default) — for Claude Desktop / similar
uv run tradingview-mcp streamable-http --host 0.0.0.0 --port 8000   # HTTP; what the Docker CMD runs
uv run tradingview-mcp check-proxy                     # probe every egress via ipinfo.io (live board on a tty, streamed lines when piped); --json for raw
TRADINGVIEW_MCP_LOG_LEVEL=DEBUG uv run tradingview-mcp # verbose logs to stderr
uv run pytest -q                                       # tests, ~50ms, no network
docker compose up -d                                   # full stack with autoheal sidecar
```

Logs go to **stderr** (stdout is reserved for the MCP transport on stdio).
Default level is `INFO` and produces a narrative line per upstream call
(`asking Yahoo Finance about AAPL`, `cache hit on sec_insider for AAPL …`).
Set `TRADINGVIEW_MCP_LOG_LEVEL` to `DEBUG` for per-feed/per-screener detail
or `WARNING` to silence routine activity. Cache directory is configurable
via `TRADINGVIEW_MCP_CACHE_DIR` (defaults to a per-user dir; the Docker
image pins it to `/var/cache/tradingview-mcp` on a named volume).

Sandbox: `stooq.com` is on the allowed-host list. Other external HTTP
hosts may need `dangerouslyDisableSandbox: true` for ad-hoc probes.

## Outbound rate limiting

`core/services/rate_limiter.py` is a process-global FIFO queue: at most one
*external* HTTP request starts every `1/RPS` seconds (default 1/s), across all
hosts, to stop upstreams blocking the egress. It's thread-safe (MCPServer runs
each sync tool in its own worker thread) and applied at the four outbound
chokepoints — so new network code is throttled automatically if it uses them:

- the proxy opener: a `_RateLimitHandler` on every `build_opener_with_proxy()`
  opener (Yahoo, Stooq, Reddit, PAP, beta-ETF, fallbacks)
- direct fetches: `rate_limiter.gated_urlopen()` (sec, bitcoin, yahoo/stooq
  fallbacks, backtest, pap fallback) — use this, not `urllib.request.urlopen`
- `tradingview_ta`: one `acquire()` in `tv_scanner._do_call` (cache miss only)
- `tradingview_screener`: an `acquire()` before each call site (RSS feeds and
  ApeWisdom ride the proxy opener instead)

Config (env): `TRADINGVIEW_MCP_RATE_LIMIT_ENABLED` (default true),
`_RPS` (default 1.0; ≤0 disables), `_MAX_WAIT` (default 30s; a caller that would
queue longer than this raises `RateLimitTimeout` instead of hanging past the MCP
client's tool timeout), `_BACKEND` (`memory`; `redis` reserved for multi-replica,
falls back to memory). `check-proxy` deliberately bypasses the limiter. Tests
disable the global limiter via `tests/unit/conftest.py`; the limiter's own
behaviour is covered with injected instances in `test_rate_limiter.py`.

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
- Don't replace the Dockerfile's `uv export --frozen … && uv pip install -r`
  with `uv pip install --system .` — that re-resolves from `pyproject.toml`
  and ignores `uv.lock`, so a new upstream major (as with `mcp` 2.0) lands
  in the image untested. Bump deps with `uv lock --upgrade-package <name>`.
