"""Shared test fixtures for the unit tier.

Keeps the suite fast and offline (~50ms target). The global outbound rate
limiter is disabled for every test so code paths that funnel through it
(``tv_scanner.ta_call``, the proxy opener, ``gated_urlopen``, …) don't actually
sleep 1s between calls. The limiter's own behaviour is covered in
``test_rate_limiter.py`` with explicitly-constructed instances, and call-site
wiring is verified by monkeypatching the local ``acquire`` references — neither
relies on the live singleton, so disabling it here is safe.
"""
from __future__ import annotations

import pytest

from tradingview_mcp.core.services import news_service as _news
from tradingview_mcp.core.services import rate_limiter as _rl
from tradingview_mcp.core.services import sentiment_service as _sent
from tradingview_mcp.core.services import tv_scanner as _tv
from tradingview_mcp.core.services import yahoo_finance_service as _yf


@pytest.fixture(autouse=True)
def _disable_global_rate_limit():
    # Pin the process-global singleton to a disabled limiter; acquire() becomes
    # a no-op. Tests that exercise the env-driven factory call
    # rate_limiter.reset_for_tests() themselves and rebuild from a clean env.
    _rl._INSTANCE = _rl.RateLimiter(enabled=False)
    yield
    _rl.reset_for_tests()


@pytest.fixture(autouse=True)
def _close_reddit_circuit():
    """Start every test with the Reddit breaker closed.

    It is process-global by design (one MCP server, one Reddit), so a test that
    exercises a full sweep failure would otherwise make the *next* test skip
    Reddit entirely and silently fall through to the live ApeWisdom path.
    """
    _sent._reset_breaker()
    yield
    _sent._reset_breaker()


@pytest.fixture(autouse=True)
def _open_tradingview_gate():
    """Start every test with the TradingView 429 gate open and no breaker.

    Same reason as the Reddit breaker: it is process-global, so one test that
    simulates a 429 storm would make every later scanner test fail fast.
    """
    _tv.reset_rate_limit_for_tests()
    yield
    _tv.reset_rate_limit_for_tests()


@pytest.fixture(autouse=True)
def _clear_request_memos():
    """Drop the short-lived request memos (TradingView per-symbol cache, RSS
    feeds, Yahoo quoteSummary block) so one test's fake upstream can't answer
    the next test's call."""
    _tv.reset_cache_for_tests()
    _news.reset_feed_cache_for_tests()
    _yf.reset_profile_cache_for_tests()
    yield
    _tv.reset_cache_for_tests()
    _news.reset_feed_cache_for_tests()
    _yf.reset_profile_cache_for_tests()
