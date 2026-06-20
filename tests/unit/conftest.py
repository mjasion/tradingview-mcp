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


@pytest.fixture(autouse=True)
def _disable_global_rate_limit():
    # Pin the process-global singleton to a disabled limiter; acquire() becomes
    # a no-op. Tests that exercise the env-driven factory call
    # rate_limiter.reset_for_tests() themselves and rebuild from a clean env.
    _rl._INSTANCE = _rl.RateLimiter(enabled=False)
    yield
    _rl.reset_for_tests()
