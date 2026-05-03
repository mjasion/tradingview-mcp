"""
Tests for Bug 1 (multi_timeframe_analysis ignores exchange parameter)
and Bug 2 (combined_analysis / coin_analysis doesn't recognise AMEX/NYSEARCA/PCX).

Root causes:
  Bug 1 — server.py constructed the TradingView symbol prefix from exchange.upper()
           instead of get_tv_exchange_prefix(exchange), so AMEX → "AMEX" was lost and
           KUCOIN was used instead (sanitize_exchange fallback).
  Bug 2 — "amex", "nysearca", "pcx" were absent from EXCHANGE_SCREENER so
           sanitize_exchange() fell back to the "kucoin" default, causing
           coin_analysis / combined_analysis to query the crypto screener.
"""
from __future__ import annotations

import pytest

from tradingview_mcp.core.utils.validators import (
    sanitize_exchange,
    get_tv_exchange_prefix,
    is_stock_exchange,
    EXCHANGE_SCREENER,
    STOCK_EXCHANGES,
)


# ── Bug 2: sanitize_exchange must recognise AMEX / NYSEARCA / PCX ─────────────

class TestSanitizeExchangeAmexAliases:
    """Bug 2 regression — AMEX/NYSEARCA/PCX must not fall back to crypto default."""

    def test_amex_is_recognised(self):
        """'AMEX' must survive sanitize_exchange, not collapse to 'kucoin'."""
        assert sanitize_exchange("AMEX", "KUCOIN") == "amex"

    def test_nysearca_is_recognised(self):
        assert sanitize_exchange("NYSEARCA", "KUCOIN") == "nysearca"

    def test_pcx_is_recognised(self):
        assert sanitize_exchange("PCX", "KUCOIN") == "pcx"

    def test_amex_lowercase_is_recognised(self):
        assert sanitize_exchange("amex", "KUCOIN") == "amex"

    def test_nysearca_lowercase_is_recognised(self):
        assert sanitize_exchange("nysearca", "KUCOIN") == "nysearca"

    def test_amex_routes_to_america_screener(self):
        """All three aliases must route to the 'america' TradingView screener."""
        assert EXCHANGE_SCREENER["amex"] == "america"
        assert EXCHANGE_SCREENER["nysearca"] == "america"
        assert EXCHANGE_SCREENER["pcx"] == "america"

    def test_amex_aliases_are_stock_exchanges(self):
        """AMEX/NYSEARCA/PCX must be classified as stock (not crypto) markets."""
        assert "amex" in STOCK_EXCHANGES
        assert "nysearca" in STOCK_EXCHANGES
        assert "pcx" in STOCK_EXCHANGES

    def test_is_stock_exchange_amex(self):
        assert is_stock_exchange("AMEX") is True
        assert is_stock_exchange("NYSEARCA") is True
        assert is_stock_exchange("PCX") is True


# ── Bug 1: get_tv_exchange_prefix must return AMEX for NYSE Arca aliases ───────

class TestGetTvExchangePrefix:
    """Bug 1 regression — symbol prefix must use TradingView's canonical code."""

    def test_amex_prefix_is_amex(self):
        """GDX lives at AMEX:GDX in TradingView, not NYSE:GDX."""
        assert get_tv_exchange_prefix("amex") == "AMEX"

    def test_nysearca_prefix_is_amex(self):
        """NYSE Arca must also map to TradingView's 'AMEX' prefix."""
        assert get_tv_exchange_prefix("nysearca") == "AMEX"

    def test_pcx_prefix_is_amex(self):
        assert get_tv_exchange_prefix("pcx") == "AMEX"

    def test_nyse_prefix_is_nyse(self):
        assert get_tv_exchange_prefix("nyse") == "NYSE"

    def test_nasdaq_prefix_is_nasdaq(self):
        assert get_tv_exchange_prefix("nasdaq") == "NASDAQ"

    def test_twse_prefix_is_twse(self):
        assert get_tv_exchange_prefix("twse") == "TWSE"

    def test_tpex_prefix_is_tpex(self):
        assert get_tv_exchange_prefix("tpex") == "TPEX"

    def test_crypto_exchange_falls_back_to_upper(self):
        """Crypto exchanges not in the map still get uppercased correctly."""
        assert get_tv_exchange_prefix("kucoin") == "KUCOIN"
        assert get_tv_exchange_prefix("binance") == "BINANCE"
        assert get_tv_exchange_prefix("mexc") == "MEXC"

    def test_full_symbol_construction_amex(self):
        """Simulate the symbol construction in server.py for AMEX exchange."""
        exchange = sanitize_exchange("AMEX", "KUCOIN")   # → "amex"
        symbol = "GDX"
        full_symbol = symbol.upper() if ":" in symbol else f"{get_tv_exchange_prefix(exchange)}:{symbol.upper()}"
        assert full_symbol == "AMEX:GDX", (
            f"Expected AMEX:GDX but got {full_symbol!r}. "
            "This means Bug 1 is not fixed: exchange prefix is wrong."
        )

    def test_full_symbol_construction_nysearca(self):
        exchange = sanitize_exchange("NYSEARCA", "KUCOIN")  # → "nysearca"
        symbol = "GDX"
        full_symbol = symbol.upper() if ":" in symbol else f"{get_tv_exchange_prefix(exchange)}:{symbol.upper()}"
        assert full_symbol == "AMEX:GDX"

    def test_full_symbol_construction_twse(self):
        """Taiwan stock 2330 (TSMC) must get TWSE prefix."""
        exchange = sanitize_exchange("TWSE", "KUCOIN")  # → "twse"
        symbol = "2330"
        full_symbol = symbol.upper() if ":" in symbol else f"{get_tv_exchange_prefix(exchange)}:{symbol.upper()}"
        assert full_symbol == "TWSE:2330"

    def test_full_symbol_construction_tpex(self):
        exchange = sanitize_exchange("TPEX", "KUCOIN")  # → "tpex"
        symbol = "3105"
        full_symbol = symbol.upper() if ":" in symbol else f"{get_tv_exchange_prefix(exchange)}:{symbol.upper()}"
        assert full_symbol == "TPEX:3105"

    def test_pre_qualified_symbol_is_not_reprefixed(self):
        """If caller already passes 'AMEX:GDX', the prefix must not be doubled."""
        exchange = sanitize_exchange("AMEX", "KUCOIN")
        symbol = "AMEX:GDX"  # already qualified
        full_symbol = symbol.upper() if ":" in symbol else f"{get_tv_exchange_prefix(exchange)}:{symbol.upper()}"
        assert full_symbol == "AMEX:GDX"


# ── Regression: existing exchanges still work ─────────────────────────────────

class TestExistingExchangesUnchanged:
    """Ensure previously-working exchanges are unaffected by the fix."""

    @pytest.mark.parametrize("exchange,expected_screener", [
        ("kucoin", "crypto"),
        ("binance", "crypto"),
        ("bybit", "crypto"),
        ("mexc", "crypto"),
        ("nasdaq", "america"),
        ("nyse", "america"),
        ("egx", "egypt"),
        ("bist", "turkey"),
        ("twse", "taiwan"),
        ("tpex", "taiwan"),
        ("sse", "china"),
        ("szse", "china"),
    ])
    def test_existing_screener_routing(self, exchange, expected_screener):
        assert EXCHANGE_SCREENER[exchange] == expected_screener

    @pytest.mark.parametrize("exchange", ["kucoin", "binance", "bybit", "mexc"])
    def test_crypto_not_in_stock_exchanges(self, exchange):
        assert exchange not in STOCK_EXCHANGES


# ── Global stock market expansion: GPW, Xetra, LSE, TSX, Euronext, Nordics, ──
#    Japan, Korea  (verified live against TradingView screener probe) ────────

class TestGlobalExchangeMappings:
    """Each new alias must:
    1. survive sanitize_exchange (i.e. be in EXCHANGE_SCREENER),
    2. route to the correct screener market,
    3. produce the TradingView prefix used in EXCH:TICKER symbols,
    4. count as a stock exchange (not crypto).
    """

    @pytest.mark.parametrize("alias,expected_market,expected_prefix", [
        ("gpw",      "poland",      "GPW"),
        ("wse",      "poland",      "GPW"),
        ("xetra",    "germany",     "XETR"),
        ("xetr",     "germany",     "XETR"),
        ("fwb",      "germany",     "FWB"),
        ("fra",      "germany",     "FWB"),
        ("lse",      "uk",          "LSE"),
        ("lon",      "uk",          "LSE"),
        ("uk",       "uk",          "LSE"),
        ("tsx",      "canada",      "TSX"),
        ("tsxv",     "canada",      "TSXV"),
        ("cse",      "canada",      "CSE"),
        ("neo",      "canada",      "NEO"),
        ("euronext", "france",      "EURONEXT"),
        ("epa",      "france",      "EURONEXT"),
        ("ams",      "netherlands", "EURONEXT"),
        ("ebr",      "belgium",     "EURONEXT"),
        ("els",      "portugal",    "EURONEXT"),
        ("mil",      "italy",       "MIL"),
        ("borsa",    "italy",       "MIL"),
        ("bme",      "spain",       "BME"),
        ("six",      "switzerland", "SIX"),
        ("vie",      "austria",     "VIE"),
        ("wbag",     "austria",     "VIE"),
        ("osl",      "norway",      "OSL"),
        ("omxsto",   "sweden",      "OMXSTO"),
        ("omxcop",   "denmark",     "OMXCOP"),
        ("omxhex",   "finland",     "OMXHEX"),
        ("tse",      "japan",       "TSE"),
        ("tyo",      "japan",       "TSE"),
        ("krx",      "korea",       "KRX"),
        ("kospi",    "korea",       "KRX"),
        ("kosdaq",   "korea",       "KOSDAQ"),
    ])
    def test_alias_maps_correctly(self, alias, expected_market, expected_prefix):
        assert sanitize_exchange(alias, "kucoin") == alias
        assert EXCHANGE_SCREENER[alias] == expected_market
        assert get_tv_exchange_prefix(alias) == expected_prefix
        assert is_stock_exchange(alias) is True
        assert alias in STOCK_EXCHANGES

    @pytest.mark.parametrize("alias", [
        "GPW", "Xetra", "LSE", "TSX", "EURONEXT", "OSL",
        "TSE", "KRX", "MIL", "SIX",
    ])
    def test_alias_case_insensitive(self, alias):
        """sanitize_exchange must normalise case so GPW, gpw, Gpw all work."""
        assert sanitize_exchange(alias, "kucoin") == alias.lower()
        assert is_stock_exchange(alias) is True
