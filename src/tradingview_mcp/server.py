"""
TradingView MCP Server — routing layer only.

Each @mcp.tool() handler is responsible for:
  1. Validating / sanitising parameters
  2. Delegating to the appropriate service module
  3. Returning the result

No business logic lives here. All computation is in core/services/*.
"""
from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import FastMCP

# Initialise the human-readable logger BEFORE importing services so they
# attach to a configured handler (otherwise the first call gets a default
# Python WARNING-only root logger).
from tradingview_mcp.core.services.log import setup as _setup_logging, log_tool_call as _log_call
_setup_logging()

# ── Service imports ────────────────────────────────────────────────────────────
from tradingview_mcp.core.services.coinlist import load_symbols
from tradingview_mcp.core.services.screener_service import (
    fetch_bollinger_analysis,
    fetch_trending_analysis,
    analyze_coin,
    scan_consecutive_candles,
    scan_advanced_candle_patterns_single_tf,
    fetch_multi_timeframe_patterns,
    run_multi_timeframe_analysis,
)
from tradingview_mcp.core.services.scanner_service import (
    volume_breakout_scan,
    volume_confirmation_analyze,
    smart_volume_scan,
)
from tradingview_mcp.core.services.multi_agent_service import run_multi_agent_analysis
from tradingview_mcp.core.services.egx_service import (
    get_egx_market_overview,
    scan_egx_sector,
    run_egx_sector_scanner,
    analyze_egx_index,
    screen_egx_stocks,
    generate_egx_trade_plan,
    analyze_egx_fibonacci,
)
from tradingview_mcp.core.services.sentiment_service import analyze_sentiment
from tradingview_mcp.core.services.news_service import fetch_news_summary
from tradingview_mcp.core.services.yahoo_finance_service import (
    get_price,
    get_market_snapshot,
    get_earnings,
    get_dividends,
)
from tradingview_mcp.core.services.stooq_service import get_price as stooq_get_price
from tradingview_mcp.core.services.bitcoin_market_service import get_bitcoin_market_pulse
from tradingview_mcp.core.services.commodity_service import get_commodity_snapshot
from tradingview_mcp.core.services.sec_service import get_insider_transactions
from tradingview_mcp.core.services.portfolio_service import portfolio_scan as _portfolio_scan
from tradingview_mcp.core.services.backtest_service import (
    run_backtest,
    compare_strategies as _compare_strategies,
    walk_forward_backtest,
)
from tradingview_mcp.core.utils.validators import (
    sanitize_timeframe,
    sanitize_exchange,
    get_tv_exchange_prefix,
)

try:
    import tradingview_screener  # noqa: F401
    TRADINGVIEW_SCREENER_AVAILABLE = True
except ImportError:
    TRADINGVIEW_SCREENER_AVAILABLE = False


# ── MCP server instance ────────────────────────────────────────────────────────

mcp = FastMCP(
    name="TradingView Multi-Market Screener",
    instructions=(
        "Multi-market screener backed by TradingView. "
        "Supports crypto exchanges (KuCoin, Binance, Bybit, MEXC, etc.) and stock markets "
        "(EGX, BIST, NASDAQ, NYSE, Bursa Malaysia, HKEX, SSE, SZSE, TWSE, TPEX). "
        "Tools: top_gainers, top_losers, bollinger_scan, coin_analysis, multi_agent_analysis, "
        "volume_breakout_scanner, egx_market_overview, egx_sector_scan, and more."
    ),
)


# ── Screener tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def top_gainers(exchange: str = "KUCOIN", timeframe: str = "15m", limit: int = 25) -> list[dict]:
    """Return top gainers for an exchange and timeframe using Bollinger Band analysis.

    Args:
        exchange: Exchange name — crypto: KUCOIN, BINANCE, BYBIT, MEXC; stocks: EGX, BIST, NASDAQ, NYSE, BURSA, HKEX, SSE, SZSE, TWSE, TPEX
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
        limit: Number of rows to return (max 50)
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    timeframe = sanitize_timeframe(timeframe, "15m")
    limit = max(1, min(limit, 50))
    rows = fetch_trending_analysis(exchange, timeframe=timeframe, limit=limit)
    return [{"symbol": r["symbol"], "changePercent": r["changePercent"], "indicators": dict(r["indicators"])} for r in rows]


@mcp.tool()
def top_losers(exchange: str = "KUCOIN", timeframe: str = "15m", limit: int = 25) -> list[dict]:
    """Return top losers for an exchange and timeframe. Supports crypto (KUCOIN, BINANCE, MEXC) and stocks (EGX, BIST, NASDAQ)."""
    exchange = sanitize_exchange(exchange, "KUCOIN")
    timeframe = sanitize_timeframe(timeframe, "15m")
    limit = max(1, min(limit, 50))
    rows = fetch_trending_analysis(exchange, timeframe=timeframe, limit=limit)
    rows.sort(key=lambda x: x["changePercent"])
    return [{"symbol": r["symbol"], "changePercent": r["changePercent"], "indicators": dict(r["indicators"])} for r in rows[:limit]]


@mcp.tool()
def bollinger_scan(exchange: str = "KUCOIN", timeframe: str = "4h", bbw_threshold: float = 0.04, limit: int = 50) -> list[dict]:
    """Scan for assets with low Bollinger Band Width (squeeze detection). Works with crypto and stocks.

    Args:
        exchange: Exchange — crypto: KUCOIN, BINANCE, BYBIT, MEXC; stocks: EGX, BIST, NASDAQ, NYSE, BURSA, HKEX, SSE, SZSE, TWSE, TPEX
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
        bbw_threshold: Maximum BBW value to filter (default 0.04)
        limit: Number of rows to return (max 100)
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    timeframe = sanitize_timeframe(timeframe, "4h")
    limit = max(1, min(limit, 100))
    rows = fetch_bollinger_analysis(exchange, timeframe=timeframe, bbw_filter=bbw_threshold, limit=limit)
    return [{"symbol": r["symbol"], "changePercent": r["changePercent"], "indicators": dict(r["indicators"])} for r in rows]


@mcp.tool()
def rating_filter(exchange: str = "KUCOIN", timeframe: str = "5m", rating: int = 2, limit: int = 25) -> list[dict]:
    """Filter coins by Bollinger Band rating.

    Args:
        exchange: Exchange name like KUCOIN, BINANCE, BYBIT, MEXC, etc.
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
        rating: BB rating (-3 to +3): -3=Strong Sell, -2=Sell, -1=Weak Sell, 1=Weak Buy, 2=Buy, 3=Strong Buy
        limit: Number of rows to return (max 50)
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    timeframe = sanitize_timeframe(timeframe, "5m")
    rating = max(-3, min(3, rating))
    limit = max(1, min(limit, 50))
    rows = fetch_trending_analysis(exchange, timeframe=timeframe, filter_type="rating", rating_filter=rating, limit=limit)
    return [{"symbol": r["symbol"], "changePercent": r["changePercent"], "indicators": dict(r["indicators"])} for r in rows]


# ── Coin / asset analysis ──────────────────────────────────────────────────────

@mcp.tool()
def coin_analysis(symbol: str, exchange: str = "KUCOIN", timeframe: str = "15m") -> dict:
    """Get detailed analysis for a specific asset (coin or stock) on specified exchange and timeframe.

    Args:
        symbol: Symbol — crypto: "BTCUSDT", "ETHUSDT"; stocks: "COMI" (EGX), "THYAO" (BIST), "600519" (SSE), "300251" (SZSE), "2330" (TWSE), "3105" (TPEX)
        exchange: Exchange — crypto: KUCOIN, BINANCE, MEXC; stocks: EGX, BIST, NASDAQ, NYSE, BURSA, HKEX, SSE, SZSE, TWSE, TPEX
        timeframe: Time interval (5m, 15m, 1h, 4h, 1D, 1W, 1M)

    Returns:
        Detailed analysis with all indicators and metrics
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    timeframe = sanitize_timeframe(timeframe, "15m")
    _log_call("coin_analysis", symbol=symbol, exchange=exchange, timeframe=timeframe)
    return analyze_coin(symbol, exchange, timeframe)


# ── Candle pattern tools ───────────────────────────────────────────────────────

@mcp.tool()
def consecutive_candles_scan(
    exchange: str = "KUCOIN",
    timeframe: str = "15m",
    pattern_type: str = "bullish",
    candle_count: int = 3,
    min_growth: float = 2.0,
    limit: int = 20,
) -> dict:
    """Scan for coins with consecutive growing/shrinking candles pattern.

    Args:
        exchange: Exchange name (BINANCE, KUCOIN, etc.)
        timeframe: Time interval (5m, 15m, 1h, 4h)
        pattern_type: "bullish" (growing candles) or "bearish" (shrinking candles)
        candle_count: Number of consecutive candles to check (2-5)
        min_growth: Minimum growth percentage for each candle
        limit: Maximum number of results to return
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    timeframe = sanitize_timeframe(timeframe, "15m")
    candle_count = max(2, min(5, candle_count))
    min_growth = max(0.5, min(20.0, min_growth))
    limit = max(1, min(50, limit))
    return scan_consecutive_candles(exchange, timeframe, pattern_type, candle_count, min_growth, limit)


@mcp.tool()
def advanced_candle_pattern(
    exchange: str = "KUCOIN",
    base_timeframe: str = "15m",
    pattern_length: int = 3,
    min_size_increase: float = 10.0,
    limit: int = 15,
) -> dict:
    """Advanced candle pattern analysis using multi-timeframe data.

    Args:
        exchange: Exchange name (BINANCE, KUCOIN, etc.)
        base_timeframe: Base timeframe for analysis (5m, 15m, 1h, 4h)
        pattern_length: Number of consecutive periods to analyse (2-4)
        min_size_increase: Minimum percentage increase in candle size
        limit: Maximum number of results to return
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    base_timeframe = sanitize_timeframe(base_timeframe, "15m")
    pattern_length = max(2, min(4, pattern_length))
    min_size_increase = max(5.0, min(50.0, min_size_increase))
    limit = max(1, min(30, limit))

    symbols = load_symbols(exchange)
    if not symbols:
        return {"error": f"No symbols found for exchange: {exchange}", "exchange": exchange}
    symbols = symbols[: min(limit * 2, 100)]

    if TRADINGVIEW_SCREENER_AVAILABLE:
        try:
            results = fetch_multi_timeframe_patterns(exchange, symbols, base_timeframe, pattern_length, min_size_increase)
            return {
                "exchange": exchange,
                "base_timeframe": base_timeframe,
                "pattern_length": pattern_length,
                "min_size_increase": min_size_increase,
                "method": "multi-timeframe",
                "total_found": len(results),
                "data": results[:limit],
            }
        except Exception:
            pass  # Fall through to single-timeframe fallback

    return scan_advanced_candle_patterns_single_tf(exchange, symbols, base_timeframe, pattern_length, min_size_increase, limit)


# ── Volume scanner tools ───────────────────────────────────────────────────────

@mcp.tool()
def volume_breakout_scanner(
    exchange: str = "KUCOIN",
    timeframe: str = "15m",
    volume_multiplier: float = 2.0,
    price_change_min: float = 3.0,
    limit: int = 25,
) -> list[dict]:
    """Detect coins with volume breakout + price breakout.

    Args:
        exchange: Exchange name like KUCOIN, BINANCE, BYBIT, MEXC, etc.
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
        volume_multiplier: How many times the volume should be above normal level (default 2.0)
        price_change_min: Minimum price change percentage (default 3.0)
        limit: Number of rows to return (max 50)
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    timeframe = sanitize_timeframe(timeframe, "15m")
    volume_multiplier = max(1.5, min(10.0, volume_multiplier))
    price_change_min = max(1.0, min(20.0, price_change_min))
    limit = max(1, min(limit, 50))
    return volume_breakout_scan(exchange, timeframe, volume_multiplier, price_change_min, limit)


@mcp.tool()
def volume_confirmation_analysis(symbol: str, exchange: str = "KUCOIN", timeframe: str = "15m") -> dict:
    """Detailed volume confirmation analysis for a specific coin.

    Args:
        symbol: Coin symbol (e.g., BTCUSDT)
        exchange: Exchange name
        timeframe: Time frame for analysis
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    timeframe = sanitize_timeframe(timeframe, "15m")
    return volume_confirmation_analyze(symbol, exchange, timeframe)


@mcp.tool()
def smart_volume_scanner(
    exchange: str = "KUCOIN",
    min_volume_ratio: float = 2.0,
    min_price_change: float = 2.0,
    rsi_range: str = "any",
    limit: int = 20,
) -> list[dict]:
    """Smart volume + technical analysis combination scanner.

    Args:
        exchange: Exchange name
        min_volume_ratio: Minimum volume multiplier (default 2.0)
        min_price_change: Minimum price change percentage (default 2.0)
        rsi_range: "oversold" (<30), "overbought" (>70), "neutral" (30-70), "any"
        limit: Number of results (max 30)
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    min_volume_ratio = max(1.2, min(10.0, min_volume_ratio))
    min_price_change = max(0.5, min(20.0, min_price_change))
    limit = max(1, min(limit, 30))
    return smart_volume_scan(exchange, min_volume_ratio, min_price_change, rsi_range, limit)


# ── Multi-agent analysis ───────────────────────────────────────────────────────

@mcp.tool()
def multi_agent_analysis(symbol: str, exchange: str = "KUCOIN", timeframe: str = "15m") -> dict:
    """Run a multi-agent debate (Technical, Sentiment, Risk) for a specific symbol.

    Args:
        symbol: Symbol — crypto: "BTCUSDT"; stocks: "COMI" (EGX), "THYAO" (BIST), "600519" (SSE), "300251" (SZSE), "2330" (TWSE), "3105" (TPEX), "GDX" (AMEX)
        exchange: Exchange — crypto: KUCOIN, BINANCE, MEXC; stocks: EGX, BIST, NASDAQ, NYSE, AMEX, NYSEARCA, PCX, SSE, SZSE, TWSE, TPEX
        timeframe: Time interval (5m, 15m, 1h, 4h, 1D, 1W)

    Returns:
        A structured debate between 3 AI agents culminating in a final trading decision.
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    timeframe = sanitize_timeframe(timeframe, "15m")
    full_symbol = symbol.upper() if ":" in symbol else f"{get_tv_exchange_prefix(exchange)}:{symbol.upper()}"
    return run_multi_agent_analysis(full_symbol, exchange, timeframe)


# ── EGX market tools ───────────────────────────────────────────────────────────

@mcp.tool()
def egx_market_overview(timeframe: str = "1D", limit: int = 10) -> dict:
    """Get a comprehensive overview of the Egyptian Exchange (EGX) market.

    Args:
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M (default 1D for stocks)
        limit: Number of stocks per category (max 20)
    """
    timeframe = sanitize_timeframe(timeframe, "1D")
    limit = max(1, min(limit, 20))
    return get_egx_market_overview(timeframe, limit)


@mcp.tool()
def egx_sector_scan(sector: str = "", timeframe: str = "1D", limit: int = 20) -> dict:
    """Scan EGX stocks by sector. Shows available sectors if none specified.

    Args:
        sector: Sector name (banks, healthcare_and_pharma, real_estate, etc.)
                Leave empty to list all sectors.
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M
        limit: Max results per sector (max 50)
    """
    timeframe = sanitize_timeframe(timeframe, "1D")
    limit = max(1, min(limit, 50))
    return scan_egx_sector(sector, timeframe, limit)


@mcp.tool()
def egx_sector_scanner(
    timeframe: str = "1D",
    top_n_sectors: int = 5,
    top_n_stocks: int = 3,
    min_stock_score: int = 60,
) -> dict:
    """Sector rotation scanner for EGX — identifies hot/cold sectors and top picks.

    Args:
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M (default 1D)
        top_n_sectors: Number of top sectors to show stock picks for (1-18, default 5)
        top_n_stocks: Number of top stocks per highlighted sector (1-10, default 3)
        min_stock_score: Minimum stock score for picks (0-100, default 60)
    """
    timeframe = sanitize_timeframe(timeframe, "1D")
    top_n_sectors = max(1, min(18, top_n_sectors))
    top_n_stocks = max(1, min(10, top_n_stocks))
    min_stock_score = max(0, min(100, min_stock_score))
    return run_egx_sector_scanner(timeframe, top_n_sectors, top_n_stocks, min_stock_score)


@mcp.tool()
def egx_index_analysis(index: str = "EGX30", timeframe: str = "1D", limit: int = 30) -> dict:
    """Analyse an EGX index showing constituent performance with full indicators.

    Args:
        index: EGX30, EGX70, EGX100, SHARIAH33, EGX35LV, TAMAYUZ
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M (default 1D)
        limit: Number of stocks to show in detail (max 100)
    """
    timeframe = sanitize_timeframe(timeframe, "1D")
    limit = max(1, min(limit, 100))
    return analyze_egx_index(index, timeframe, limit)


@mcp.tool()
def egx_stock_screener(
    timeframe: str = "1D",
    min_score: int = 55,
    index_filter: str = "",
    limit: int = 20,
) -> dict:
    """Production stock ranking engine for EGX — finds strong stocks with actionable setups.

    Args:
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M (default 1D)
        min_score: Minimum stock score to include (0-100, default 55)
        index_filter: Filter by index — EGX30, EGX70, EGX100, SHARIAH33, EGX35LV, TAMAYUZ
        limit: Number of results (max 50)
    """
    timeframe = sanitize_timeframe(timeframe, "1D")
    min_score = max(0, min(100, min_score))
    limit = max(1, min(50, limit))
    return screen_egx_stocks(timeframe, min_score, index_filter, limit)


@mcp.tool()
def egx_trade_plan(symbol: str, timeframe: str = "1D") -> dict:
    """Generate a full trade plan for a specific EGX stock.

    Args:
        symbol: EGX stock symbol (e.g., "COMI", "TMGH", "FWRY")
        timeframe: One of 5m, 15m, 1h, 4h, 1D, 1W, 1M (default 1D)
    """
    timeframe = sanitize_timeframe(timeframe, "1D")
    return generate_egx_trade_plan(symbol, timeframe)


@mcp.tool()
def egx_fibonacci_retracement(symbol: str, lookback: str = "52W", timeframe: str = "1D") -> dict:
    """Fibonacci retracement analysis for EGX stocks.

    Args:
        symbol: EGX stock symbol (e.g., "COMI", "TMGH", "FWRY")
        lookback: Period for swing high/low — "1M", "3M", "6M", "52W", "ALL" (default 52W)
        timeframe: Analysis timeframe (5m, 15m, 1h, 4h, 1D, 1W, 1M — default 1D)
    """
    timeframe = sanitize_timeframe(timeframe, "1D")
    lookback = lookback.strip().upper()
    return analyze_egx_fibonacci(symbol, lookback, timeframe)


# ── Multi-timeframe analysis ───────────────────────────────────────────────────

@mcp.tool()
def multi_timeframe_analysis(symbol: str, exchange: str = "KUCOIN") -> dict:
    """Multi-timeframe alignment analysis (Weekly → Daily → 4H → 1H → 15m).

    Args:
        symbol: Symbol — crypto: "BTCUSDT"; stocks: "COMI" (EGX), "THYAO" (BIST), "600519" (SSE), "300251" (SZSE), "2330" (TWSE), "3105" (TPEX), "GDX" (AMEX)
        exchange: Exchange — crypto: KUCOIN, BINANCE, MEXC; stocks: EGX, BIST, NASDAQ, NYSE, AMEX, NYSEARCA, PCX, SSE, SZSE, TWSE, TPEX
    """
    exchange = sanitize_exchange(exchange, "KUCOIN")
    full_symbol = symbol.upper() if ":" in symbol else f"{get_tv_exchange_prefix(exchange)}:{symbol.upper()}"
    return run_multi_timeframe_analysis(full_symbol, exchange)


# ── Sentiment & news tools ─────────────────────────────────────────────────────

@mcp.tool()
def market_sentiment(symbol: str, category: str = "all", limit: int = 20) -> dict:
    """Real-time Reddit sentiment analysis for stocks and crypto.

    Args:
        symbol: Asset symbol ("AAPL", "BTC", "ETH", "TSLA")
        category: Subreddit group to search ("crypto", "stocks", "all")
        limit: Number of posts to analyse
    """
    return analyze_sentiment(symbol, category, limit)


_GPW_EXCHANGES = {"gpw", "wse"}
_CRYPTO_EXCHANGES = {"binance", "kucoin", "bybit", "mexc", "bitget", "okx",
                     "coinbase", "gateio", "huobi", "bitfinex", "kraken", "bitstamp"}


def _news_category_for_exchange(exchange: str) -> str:
    """Pick the RSS feed group for a given exchange.

    GPW → Polish-language feeds (Bankier, Money.pl, Comparic) — TradingView's
    English/Reuters feeds rarely cover Polish small/mid caps.
    Crypto exchanges → "crypto". Everything else → "stocks".
    """
    ex = exchange.strip().lower()
    if ex in _GPW_EXCHANGES:
        return "pl_stocks"
    if ex in _CRYPTO_EXCHANGES:
        return "crypto"
    return "stocks"


@mcp.tool()
def financial_news(
    symbol: str = None,
    category: str = "stocks",
    limit: int = 10,
    exchange: str = None,
) -> dict:
    """Real-time financial news from RSS feeds.

    Args:
        symbol: Optional symbol filter ("AAPL", "BTC", "KGH"). None = all news.
        category: Feed category ("crypto", "stocks", "pl_stocks", "all").
                  Ignored if ``exchange`` is provided.
        limit: Max number of news items
        exchange: Optional exchange code. When given, overrides ``category``:
                  GPW/WSE → "pl_stocks", crypto exchanges → "crypto",
                  everything else → "stocks". For Polish tickers the symbol
                  is automatically expanded to company-name aliases (KGH →
                  "KGHM" / "Polska Miedź").
    """
    effective_category = _news_category_for_exchange(exchange) if exchange else category
    return fetch_news_summary(symbol, effective_category, limit)


@mcp.tool()
def combined_analysis(symbol: str, exchange: str = "NASDAQ", timeframe: str = "1D") -> dict:
    """POWER TOOL: TradingView technical analysis + sentiment + Financial news.

    For US/EU/Asia-Pacific exchanges, sentiment uses Reddit.
    For GPW/WSE (Warsaw), Reddit is skipped (poor Polish coverage) and news is
    pulled from Polish-language feeds (Bankier, Money.pl, Comparic) instead.

    Args:
        symbol: Asset symbol ("AAPL", "BTCUSDT", "KGH", "SAP")
        exchange: Exchange code (NASDAQ, NYSE, AMEX, GPW, XETRA, LSE, TSX,
                  EURONEXT, MIL, BME, SIX, OSL, OMXSTO, TSE, KRX,
                  BINANCE, KUCOIN, ...)
        timeframe: Analysis timeframe (5m, 15m, 1h, 4h, 1D, 1W)
    """
    ex_lower = exchange.strip().lower()
    is_gpw = ex_lower in _GPW_EXCHANGES
    is_crypto = ex_lower in _CRYPTO_EXCHANGES

    tech = coin_analysis(symbol, exchange, timeframe)
    news_category = _news_category_for_exchange(exchange)
    news = fetch_news_summary(symbol, category=news_category, limit=5)

    if is_gpw:
        sentiment = {
            "skipped": True,
            "reason": (
                "Reddit ma znikomy ruch o spółkach z GPW; liczby są niereprezentatywne. "
                "Sentyment dla polskich spółek lepiej oceniać przez polskie źródła "
                "(bankier.pl, parkiet.com, biznesradar.pl, gpw.pl/ESPI)."
            ),
            "fallback": "rss_pl",
            "fallback_news_count": news.get("count", 0),
        }
        signals_agree = None
        confidence = "TECHNICAL_ONLY"
        recommendation = (
            f"Technical {tech.get('market_sentiment', {}).get('buy_sell_signal', 'N/A') if isinstance(tech, dict) else 'N/A'} "
            f"— sentiment skipped for GPW, see Polish RSS news ({news.get('count', 0)} headlines)"
        )
    else:
        sent_cat = "crypto" if is_crypto else "stocks"
        sentiment = analyze_sentiment(symbol, category=sent_cat)
        tech_momentum = tech.get("market_sentiment", {}).get("momentum", "") if isinstance(tech, dict) else ""
        tech_bullish = tech_momentum == "Bullish"
        sent_bullish = sentiment.get("sentiment_score", 0) > 0.1
        signals_agree = tech_bullish == sent_bullish
        confidence = "HIGH" if signals_agree else "MIXED"
        tech_signal = tech.get("market_sentiment", {}).get("buy_sell_signal", "N/A") if isinstance(tech, dict) else "N/A"
        recommendation = (
            f"Technical {tech_signal} "
            f"{'confirmed by' if signals_agree else 'conflicts with'} "
            f"{sentiment.get('sentiment_label', 'Neutral')} Reddit sentiment "
            f"({sentiment.get('posts_analyzed', 0)} posts analyzed)"
        )

    return {
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "technical": tech,
        "sentiment": sentiment,
        "news": {"count": news.get("count", 0), "latest": news.get("items", [])[:3]},
        "confluence": {
            "signals_agree": signals_agree,
            "confidence": confidence,
            "recommendation": recommendation,
        },
    }


# ── Backtest tools ─────────────────────────────────────────────────────────────

@mcp.tool()
def backtest_strategy(
    symbol: str,
    strategy: str,
    period: str = "1y",
    initial_capital: float = 10000.0,
    commission_pct: float = 0.1,
    slippage_pct: float = 0.05,
    interval: str = "1d",
    include_trade_log: bool = False,
    include_equity_curve: bool = False,
) -> dict:
    """Backtest a trading strategy on historical data with institutional-grade metrics.

    Args:
        symbol: Yahoo Finance symbol (AAPL, BTC-USD, THYAO.IS, ^GSPC)
        strategy: rsi | bollinger | macd | ema_cross | supertrend | donchian
        period: '1mo', '3mo', '6mo', '1y', '2y'
        initial_capital: Starting capital in USD (default $10,000)
        commission_pct: Per-trade commission % (default 0.1%)
        slippage_pct: Per-trade slippage % (default 0.05%)
        interval: '1d' (daily) or '1h' (hourly)
        include_trade_log: Include full per-trade log (default False)
        include_equity_curve: Include equity curve data points (default False)
    """
    return run_backtest(
        symbol, strategy, period, initial_capital,
        commission_pct, slippage_pct, interval,
        include_trade_log, include_equity_curve,
    )


@mcp.tool()
def compare_strategies(
    symbol: str,
    period: str = "1y",
    initial_capital: float = 10000.0,
    interval: str = "1d",
) -> dict:
    """Run all 6 strategies (RSI, Bollinger, MACD, EMA Cross, Supertrend, Donchian) and return a ranked leaderboard.

    Args:
        symbol: Yahoo Finance symbol (AAPL, BTC-USD, SPY…)
        period: '1mo', '3mo', '6mo', '1y', '2y'
        initial_capital: Starting capital in USD (default $10,000)
        interval: '1d' (daily) or '1h' (hourly)
    """
    return _compare_strategies(symbol, period, initial_capital, interval=interval)


@mcp.tool()
def walk_forward_backtest_strategy(
    symbol: str,
    strategy: str,
    period: str = "2y",
    initial_capital: float = 10000.0,
    commission_pct: float = 0.1,
    slippage_pct: float = 0.05,
    n_splits: int = 3,
    train_ratio: float = 0.7,
    interval: str = "1d",
) -> dict:
    """Walk-forward backtest to detect overfitting — validates strategy on unseen data.

    Args:
        symbol: Yahoo Finance symbol (AAPL, BTC-USD, SPY…)
        strategy: rsi | bollinger | macd | ema_cross | supertrend | donchian
        period: '1mo', '3mo', '6mo', '1y', '2y' (recommend '2y')
        initial_capital: Starting capital per fold in USD (default $10,000)
        commission_pct: Per-trade commission % (default 0.1%)
        slippage_pct: Per-trade slippage % (default 0.05%)
        n_splits: Number of walk-forward folds (default 3, max 10)
        train_ratio: Fraction of each fold used for training (default 0.7)
        interval: '1d' (daily) or '1h' (hourly)
    """
    return walk_forward_backtest(
        symbol, strategy, period, initial_capital,
        commission_pct, slippage_pct, n_splits, train_ratio, interval,
    )


# ── Yahoo Finance tools ────────────────────────────────────────────────────────

# Curated set of pure-GPW tickers that should route to Stooq.
# Kept narrow on purpose: GPW also lists dual-listed US CFDs (AAPL, MSFT, …)
# which we do NOT want to send to Stooq — Yahoo serves them correctly.
_STOOQ_PRIMARY_GPW_TICKERS: set[str] = {
    # WIG20 / mWIG40 — Polish blue chips
    "KGH", "CDR", "JSW", "PKN", "PZU", "PEO", "PKO", "DNP", "ALR", "LPP",
    "ALE", "OPL", "SPL", "MBK", "ASE", "TPE", "PGE", "CCC", "KTY", "KRU",
    "CPS", "EUR", "BDX", "ATT",
    # Smaller caps in user's portfolio
    "CRI", "CRQ",
    # BETA ETFs — Stooq covers these via the ``.pl`` market-suffix form,
    # resolved automatically by stooq_service._candidate_symbols.
    "ETFBW20TR", "ETFBCASH", "ETFBS80TR", "ETFBM40TR", "ETFBSPXPL", "ETFBNDQPL",
}


def _should_route_to_stooq(symbol: str) -> bool:
    """True if *symbol* is unambiguously a Warsaw-listed Polish stock."""
    s = symbol.strip().upper()
    if s.endswith(".WA"):
        return True
    return s in _STOOQ_PRIMARY_GPW_TICKERS


@mcp.tool()
def yahoo_price(symbol: str) -> dict:
    """Real-time price quote — Yahoo Finance globally, Stooq for Polish (GPW) tickers.

    Yahoo Finance does not reliably cover Warsaw Stock Exchange tickers
    (``KGHM.WA`` returns null). For symbols ending in ``.WA`` or matching a
    curated set of pure-GPW codes (KGH, CDR, JSW, PKN, ETFBW20TR, …), the
    request is routed to Stooq instead. All other symbols — including
    GPW-listed CFDs of US stocks (AAPL, MSFT) — use Yahoo Finance.

    Args:
        symbol: e.g. AAPL, BTC-USD, SPY, ^GSPC, EURUSD=X, THYAO.IS, KGHM.WA, KGH
    """
    if _should_route_to_stooq(symbol):
        return stooq_get_price(symbol)
    return get_price(symbol)


@mcp.tool()
def market_snapshot() -> dict:
    """Global market overview: major indices, top crypto, FX rates, and key ETFs.
    Powered by Yahoo Finance.
    """
    return get_market_snapshot()


@mcp.tool()
def commodity_snapshot() -> dict:
    """Single-call commodity dashboard: copper, gold, silver, oil (WTI/Brent),
    natural gas, USD index — for grounding stock-thesis reasoning.

    Use this whenever a portfolio question hinges on raw materials:
      - KGHM thesis → check copper trend
      - PKN Orlen / Lotos thesis → check WTI/Brent
      - JSW thesis → coking coal (NOT covered — see notes)
      - Any USD-sensitive trade → check DXY

    Returns price, 24h change %, RSI, EMA50 position, and a one-word trend
    tag (uptrend/downtrend/overbought/oversold/etc.) per commodity. Failed
    symbols are returned as ``null`` rather than raising — partial dashboard
    is better than no dashboard.

    Source: TradingView (free public endpoint via tradingview-ta).
    """
    _log_call("commodity_snapshot")
    return get_commodity_snapshot()


@mcp.tool()
def next_earnings(symbol: str) -> dict:
    """Earnings calendar + recent EPS-surprise history for a US/global stock.

    Use this BEFORE recommending a buy/add to avoid landing right before
    an earnings release (a 2-day pre-earnings entry sees materially higher
    drawdown risk than the rest of the quarter).

    Args:
        symbol: Yahoo Finance ticker — e.g. ``AAPL``, ``MSFT``, ``TSLA``,
                ``THYAO.IS``. Polish (.WA) tickers are not covered by Yahoo's
                earnings endpoint; use ``financial_news(category="pl_stocks")``
                for GPW companies.

    Returns:
        ``{symbol, next_earnings_date, days_until, history: [...], source}``
        — or ``{symbol, error, source}`` on rate-limit / coverage gap.
    """
    _log_call("next_earnings", symbol=symbol)
    return get_earnings(symbol)


@mcp.tool()
def dividend_history(symbol: str) -> dict:
    """Forward dividend yield + ex-date + payout history for a US/global stock.

    Args:
        symbol: Yahoo Finance ticker — e.g. ``DVN``, ``KO``, ``JNJ``.
                Polish (.WA) tickers are not covered by this endpoint.

    Returns:
        ``{dividend_yield, ex_dividend_date, next_ex_date, next_dividend_date,
        payout_ratio, five_year_avg_yield, last_dividend_value/date, source}``
        — or ``{symbol, error, source}`` on rate-limit / coverage gap.
    """
    _log_call("dividend_history", symbol=symbol)
    return get_dividends(symbol)


@mcp.tool()
def insider_transactions(symbol: str, limit: int = 10) -> dict:
    """Recent insider trades (SEC Form 4) for a US-listed stock.

    Form 4 captures insider buys/sells (officers, directors, 10%+ holders).
    Useful as a fundamental signal — "the CFO just bought 50k shares" is
    a different setup than "three directors trimmed positions in two weeks".

    Args:
        symbol: US ticker tracked by SEC EDGAR (AAPL, MSFT, TSLA, NVDA…).
                Non-US tickers (GPW .WA, .IS, .L, .DE) are NOT covered.
        limit:  Max recent Form-4 filings to return (default 10).

    Returns:
        ``{symbol, cik, name, filings: [{date, accession, url}], count, source}``
        Each ``url`` links to the filing landing page on sec.gov so a follow-up
        question can drill into the transaction breakdown.
        On unknown ticker / network error returns an ``error`` field.
    """
    _log_call("insider_transactions", symbol=symbol, limit=limit)
    return get_insider_transactions(symbol, limit=limit)


@mcp.tool()
def portfolio_scan(
    symbols: list[str],
    exchange: str = "NASDAQ",
    timeframe: str = "1D",
    news_category: str = "stocks",
    include_insider: bool = False,
) -> dict:
    """Batch-scan a watchlist for things worth your attention right now.

    For each symbol fans out (in parallel) calls to TA + earnings + dividends
    + recent news, then surfaces compact ``flags`` such as ``rsi_overbought``,
    ``earnings_in_3d``, ``ex_dividend_in_5d``, ``volatility_high``,
    ``news_active(8)``.

    Use this instead of looping ``coin_analysis`` + ``next_earnings`` +
    ``dividend_history`` per symbol — one call returns the full dashboard.

    Args:
        symbols: tickers to scan (e.g. ``["AAPL","MSFT","NVDA"]``).
        exchange: exchange routing — ``NASDAQ``, ``NYSE``, ``GPW``, ``BIST``…
        timeframe: TA timeframe (``5m``..``1M``); default ``1D``.
        news_category: feed group — ``"stocks"`` for US, ``"pl_stocks"`` for GPW,
                       ``"crypto"`` for crypto, ``"all"`` to merge.
        include_insider: when True, also fetch SEC Form-4 counts (US only,
                         adds ~1s/symbol, off by default).

    Returns:
        ``{results: [{symbol, price, rsi, flags, ...}], summary, source}``.
    """
    _log_call("portfolio_scan", symbols=symbols, exchange=exchange,
              timeframe=timeframe, include_insider=include_insider)
    return _portfolio_scan(
        symbols, exchange=exchange, timeframe=timeframe,
        news_category=news_category, include_insider=include_insider,
    )


@mcp.tool()
def bitcoin_market_pulse() -> dict:
    """Single-call BTC macro context: price, dominance, total market cap + risk assessment.

    Use this WHENEVER analyzing any cryptocurrency (altcoin or BTC itself) to
    get the broader market frame in one shot. A SOL/ETH/whatever setup looks
    very different when BTC is dumping with rising dominance vs. when alts
    are leading. Calling this once gives Claude the macro context to provide
    Bitcoin-aware commentary alongside the per-coin analysis - without
    chaining 2-3 separate yahoo_price + manual reasoning calls.

    Returns:
      - bitcoin: price, 24h change %, volume, market cap
      - dominance: BTC and ETH market-cap share of total crypto
      - total_market: total crypto mcap + 24h change + active coin count
      - assessment: label (HIGH_RISK / ALT_RISK / ALT_FAVORABLE / OPPORTUNITY_WITH_CAUTION / NEUTRAL) + 1-paragraph reasoning
    """
    return get_bitcoin_market_pulse()


# ── Resource ───────────────────────────────────────────────────────────────────

@mcp.resource("exchanges://list")
def exchanges_list() -> str:
    """List available exchanges from the coinlist directory."""
    try:
        current_dir = os.path.dirname(__file__)
        coinlist_dir = os.path.join(current_dir, "coinlist")
        if os.path.exists(coinlist_dir):
            exchanges = [
                f[:-4].upper()
                for f in os.listdir(coinlist_dir)
                if f.endswith(".txt")
            ]
            if exchanges:
                return f"Available exchanges: {', '.join(sorted(exchanges))}"
    except Exception:
        pass
    return "Common exchanges: KUCOIN, BINANCE, BYBIT, MEXC, BITGET, OKX, COINBASE, GATEIO, HUOBI, BITFINEX, KRAKEN, BITSTAMP, BIST, EGX, NASDAQ, TWSE, TPEX"


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="TradingView Screener MCP server")
    parser.add_argument(
        "transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        nargs="?",
        help="Transport (default stdio)",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    if os.environ.get("DEBUG_MCP"):
        import sys
        print(f"[DEBUG_MCP] pkg cwd={os.getcwd()} argv={sys.argv} file={__file__}", file=sys.stderr, flush=True)

    if args.transport == "stdio":
        mcp.run()
    else:
        try:
            mcp.settings.host = args.host
            mcp.settings.port = args.port
        except Exception:
            pass
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
