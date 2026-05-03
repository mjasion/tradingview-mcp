from __future__ import annotations
import os
from typing import Set

ALLOWED_TIMEFRAMES: Set[str] = {"5m", "15m", "1h", "4h", "1D", "1W", "1M"}
_TIMEFRAME_ALIASES = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
    "1w": "1W",
    "1m": "1M",
}

# Exchanges that represent stock markets (not crypto)
STOCK_EXCHANGES: Set[str] = {
    "egx", "bist", "nasdaq", "nyse",
    "amex", "nysearca", "pcx",          # NYSE Arca / AMEX (ETFs: GDX, GLD, XLE, SPY, QQQ, etc.)
    "bursa", "myx", "klse", "ace", "leap",
    "hkex", "hk", "hsi",
    "asx",
    "sse", "szse", "chn",
    "twse", "tpex",
    # Europe
    "gpw", "wse",                                            # Warsaw (Poland)
    "xetra", "xetr", "fwb", "fra",                           # Germany
    "lse", "lon", "uk",                                      # London (UK)
    "euronext", "epa", "par", "ams", "ena",                  # Euronext Paris/Amsterdam
    "ebr", "bru", "els", "lis",                              # Euronext Brussels/Lisbon
    "mil", "borsa", "bit",                                   # Borsa Italiana
    "bme", "mce",                                            # Spain
    "six", "swx", "ebs",                                     # Switzerland
    "vie", "wbag",                                           # Vienna (Austria)
    # Nordics
    "osl", "ose",                                            # Oslo (Norway)
    "omxsto", "sto", "ome",                                  # Stockholm (Sweden)
    "omxcop", "cph", "cse_dk",                               # Copenhagen (Denmark)
    "omxhex", "hel",                                         # Helsinki (Finland)
    # Americas
    "tsx", "tsxv", "cse", "neo",                             # Canada
    # Asia
    "tse", "tyo", "jpx",                                     # Tokyo (Japan)
    "krx", "kospi", "kosdaq",                                # Korea
}

EXCHANGE_SCREENER = {
    "all": "crypto",
    "huobi": "crypto",
    "kucoin": "crypto",
    "coinbase": "crypto",
    "gateio": "crypto",
    "binance": "crypto",
    "bitfinex": "crypto",
    "bitget": "crypto",
    "bybit": "crypto",
    "okx": "crypto",
    "mexc": "crypto",
    "bist": "turkey",
    # Egyptian Stock Market Support
    "egx": "egypt",
    "nasdaq": "america",
    # Malaysia Stock Market Support
    "bursa": "malaysia",
    "myx": "malaysia",
    "klse": "malaysia",
    "ace": "malaysia",      # ACE Market (Access, Certainty, Efficiency)
    "leap": "malaysia",     # LEAP Market (Leading Entrepreneur Accelerator Platform)
    # Hong Kong Stock Market Support
    "hkex": "hongkong",     # Hong Kong Exchange
    "hk": "hongkong",       # Hong Kong (alternate)
    "hsi": "hongkong",      # Hang Seng Index constituents
    "nyse": "america",
    # NYSE Arca / AMEX — ETFs (GDX, GLD, XLE, SPY, QQQ …) are listed here in TradingView
    "amex": "america",      # TradingView canonical prefix for NYSE Arca ETFs
    "nysearca": "america",  # alias: NYSE Arca (official name used by issuers)
    "pcx": "america",       # alias: Pacific Exchange (historical MIC code for NYSE Arca)
    "asx": "australia",     # Australian Securities Exchange
    # China A-Share Market Support
    "sse": "china",         # Shanghai Stock Exchange (上海证券交易所)
    "szse": "china",        # Shenzhen Stock Exchange (深圳证券交易所)
    "chn": "china",         # China A-shares (combined alias)
    # Taiwan Stock Market Support
    "twse": "taiwan",       # Taiwan Stock Exchange (臺灣證券交易所)
    "tpex": "taiwan",       # Taipei Exchange (櫃買中心, OTC market)
    # Europe
    "gpw": "poland", "wse": "poland",                                       # Warsaw Stock Exchange
    "xetra": "germany", "xetr": "germany", "fwb": "germany", "fra": "germany",
    "lse": "uk", "lon": "uk", "uk": "uk",                                   # London Stock Exchange
    "euronext": "france", "epa": "france", "par": "france",                 # Euronext Paris (default)
    "ams": "netherlands", "ena": "netherlands",                             # Euronext Amsterdam
    "ebr": "belgium", "bru": "belgium",                                     # Euronext Brussels
    "els": "portugal", "lis": "portugal",                                   # Euronext Lisbon
    "mil": "italy", "borsa": "italy", "bit": "italy",                       # Borsa Italiana (Milan)
    "bme": "spain", "mce": "spain",                                         # Bolsa de Madrid
    "six": "switzerland", "swx": "switzerland", "ebs": "switzerland",       # SIX Swiss Exchange
    "vie": "austria", "wbag": "austria",                                    # Vienna Stock Exchange
    # Nordics
    "osl": "norway", "ose": "norway",                                       # Oslo Bors
    "omxsto": "sweden", "sto": "sweden", "ome": "sweden",                   # Nasdaq Stockholm
    "omxcop": "denmark", "cph": "denmark", "cse_dk": "denmark",             # Nasdaq Copenhagen
    "omxhex": "finland", "hel": "finland",                                  # Nasdaq Helsinki
    # Canada
    "tsx": "canada", "tsxv": "canada", "cse": "canada", "neo": "canada",
    # Japan
    "tse": "japan", "tyo": "japan", "jpx": "japan",                         # Tokyo Stock Exchange
    # Korea
    "krx": "korea", "kospi": "korea", "kosdaq": "korea",                    # KRX (KOSPI + KOSDAQ)
}

# Map validated exchange identifiers to their canonical TradingView symbol prefix.
# TradingView uses "AMEX" as the prefix for all NYSE Arca / ETF listings; passing
# "NYSE:GDX" returns no data even though GDX trades on NYSE Arca.
_EXCHANGE_TV_PREFIX: dict = {
    "amex": "AMEX",
    "nysearca": "AMEX",
    "pcx": "AMEX",
    "nasdaq": "NASDAQ",
    "nyse": "NYSE",
    "egx": "EGX",
    "bist": "BIST",
    "bursa": "MYX",
    "myx": "MYX",
    "klse": "MYX",
    "ace": "MYX",
    "leap": "MYX",
    "hkex": "HKEX",
    "hk": "HKEX",
    "hsi": "HSI",
    "asx": "ASX",
    "sse": "SSE",
    "szse": "SZSE",
    "chn": "SSE",
    "twse": "TWSE",
    "tpex": "TPEX",
    # Europe — confirmed via TradingView screener probe
    "gpw": "GPW", "wse": "GPW",
    "xetra": "XETR", "xetr": "XETR", "fwb": "FWB", "fra": "FWB",
    "lse": "LSE", "lon": "LSE", "uk": "LSE",
    # Euronext is one prefix for FR/NL/BE/PT in TradingView even though screener splits them
    "euronext": "EURONEXT", "epa": "EURONEXT", "par": "EURONEXT",
    "ams": "EURONEXT", "ena": "EURONEXT",
    "ebr": "EURONEXT", "bru": "EURONEXT",
    "els": "EURONEXT", "lis": "EURONEXT",
    "mil": "MIL", "borsa": "MIL", "bit": "MIL",
    "bme": "BME", "mce": "BME",
    "six": "SIX", "swx": "SIX", "ebs": "SIX",
    "vie": "VIE", "wbag": "VIE",
    # Nordics
    "osl": "OSL", "ose": "OSL",
    "omxsto": "OMXSTO", "sto": "OMXSTO", "ome": "OMXSTO",
    "omxcop": "OMXCOP", "cph": "OMXCOP", "cse_dk": "OMXCOP",
    "omxhex": "OMXHEX", "hel": "OMXHEX",
    # Canada
    "tsx": "TSX", "tsxv": "TSXV", "cse": "CSE", "neo": "NEO",
    # Japan
    "tse": "TSE", "tyo": "TSE", "jpx": "TSE",
    # Korea — KRX prefix covers KOSPI; KOSDAQ tickers use separate prefix
    "krx": "KRX", "kospi": "KRX", "kosdaq": "KOSDAQ",
}


def get_tv_exchange_prefix(exchange: str) -> str:
    """Return the TradingView symbol prefix for *exchange* (e.g. ``AMEX`` for ``nysearca``).

    Falls back to ``exchange.upper()`` for exchanges not in the explicit map so
    that crypto exchanges (KUCOIN, BINANCE, …) still work as before.
    """
    return _EXCHANGE_TV_PREFIX.get(exchange.strip().lower(), exchange.upper())

# Get absolute path to coinlist directory relative to this module
# This file is at: src/tradingview_mcp/core/utils/validators.py
# We want: src/tradingview_mcp/coinlist/
_this_file = __file__
_utils_dir = os.path.dirname(_this_file)  # core/utils
_core_dir = os.path.dirname(_utils_dir)   # core  
_package_dir = os.path.dirname(_core_dir) # tradingview_mcp
COINLIST_DIR = os.path.join(_package_dir, 'coinlist')


def sanitize_timeframe(tf: str, default: str = "5m") -> str:
    if not tf:
        return default
    normalized = tf.strip().lower()
    return _TIMEFRAME_ALIASES.get(normalized, default)


def sanitize_exchange(ex: str, default: str = "kucoin") -> str:
    if not ex:
        return default
    exs = ex.strip().lower()
    return exs if exs in EXCHANGE_SCREENER else default


def is_stock_exchange(exchange: str) -> bool:
    """Return True if the exchange is a stock market (not crypto)."""
    return exchange.strip().lower() in STOCK_EXCHANGES


def get_market_type(exchange: str) -> str:
    """Return the TradingView market type for screener queries."""
    return EXCHANGE_SCREENER.get(exchange.strip().lower(), "crypto")
