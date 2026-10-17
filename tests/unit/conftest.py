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

from tradingview_mcp.core.services import rate_limiter as _rl
from tradingview_mcp.core.services import sentiment_service as _sent


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
