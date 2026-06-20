"""Tests for the global outbound rate limiter.

All deterministic and offline: the clock is a fake function and ``sleep_fn``
just records its argument, so nothing actually waits. The one threaded test
proves the lock never double-books a slot — it still finishes in milliseconds
because the fake sleep is a no-op.
"""
from __future__ import annotations

import threading
import urllib.request

import pytest

from tradingview_mcp.core.services import rate_limiter as rl
from tradingview_mcp.core.services import proxy_manager as pm


def _limiter(*, rps=1.0, max_wait=0.0, enabled=True, clock=None):
    """A limiter wired to a fake clock and a recording sleep.

    Returns ``(limiter, waits)`` where ``waits`` collects every value passed to
    ``sleep_fn`` (i.e. the actual blocking durations).
    """
    if clock is None:
        clock = [0.0]
    waits: list[float] = []
    backend = rl.MemoryBackend(time_fn=lambda: clock[0])
    limiter = rl.RateLimiter(
        rps=rps, max_wait=max_wait, enabled=enabled,
        backend=backend, sleep_fn=waits.append,
    )
    return limiter, waits


# ── Slot scheduling ───────────────────────────────────────────────────────────

def test_sequential_calls_are_spaced_one_interval_apart():
    limiter, waits = _limiter(rps=1.0)  # interval = 1s, clock frozen at 0
    returned = [limiter.acquire() for _ in range(4)]
    # First slot is free; each subsequent caller waits one more interval.
    assert returned == [0.0, 1.0, 2.0, 3.0]
    assert waits == [1.0, 2.0, 3.0]  # the 0.0 wait never calls sleep


def test_rps_changes_the_interval():
    limiter, _ = _limiter(rps=2.0)  # 2 req/s → 0.5s interval
    assert [limiter.acquire() for _ in range(3)] == [0.0, 0.5, 1.0]


def test_idle_time_drains_the_queue():
    clock = [0.0]
    limiter, _ = _limiter(rps=1.0, clock=clock)
    assert limiter.acquire() == 0.0          # reserves slot at t=1
    clock[0] = 10.0                          # plenty of idle time passes
    assert limiter.acquire() == 0.0          # no backlog → no wait


# ── Wait cap (shed instead of hang) ──────────────────────────────────────────

def test_max_wait_sheds_without_reserving_a_slot():
    limiter, _ = _limiter(rps=1.0, max_wait=2.5)
    assert [limiter.acquire() for _ in range(3)] == [0.0, 1.0, 2.0]  # next_at now 3
    with pytest.raises(rl.RateLimitTimeout) as ei:
        limiter.acquire("yahoo")          # would wait 3.0s > 2.5s
    assert ei.value.wait == pytest.approx(3.0)
    assert ei.value.max_wait == 2.5
    assert "yahoo" in str(ei.value)
    # The shed request must NOT have consumed a slot: a retry sees the same depth.
    with pytest.raises(rl.RateLimitTimeout):
        limiter.acquire()


# ── No-op modes ──────────────────────────────────────────────────────────────

def test_disabled_limiter_is_a_noop():
    limiter, waits = _limiter(enabled=False)
    assert [limiter.acquire() for _ in range(5)] == [0.0] * 5
    assert waits == []


def test_non_positive_rps_disables_throttling():
    limiter, waits = _limiter(rps=0.0)
    assert limiter.acquire() == 0.0
    assert waits == []


# ── Thread safety: the lock never hands two callers the same slot ─────────────

def test_concurrent_acquire_reserves_distinct_slots():
    # Clock frozen at 0 + no-op sleep → every caller's wait equals the slot
    # index it reserved. If the lock were missing, two threads could read the
    # same next_at and return a duplicate wait.
    limiter, _ = _limiter(rps=1.0)
    n = 32
    results: list[float] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()                    # maximise contention
        w = limiter.acquire()
        with results_lock:
            results.append(w)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [float(i) for i in range(n)]  # 0..n-1, each once


# ── Env-driven factory ───────────────────────────────────────────────────────

@pytest.fixture
def clean_limiter_env(monkeypatch):
    for var in ("TRADINGVIEW_MCP_RATE_LIMIT_ENABLED",
                "TRADINGVIEW_MCP_RATE_LIMIT_RPS",
                "TRADINGVIEW_MCP_RATE_LIMIT_MAX_WAIT",
                "TRADINGVIEW_MCP_RATE_LIMIT_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    rl.reset_for_tests()
    yield monkeypatch
    rl.reset_for_tests()


def test_factory_defaults(clean_limiter_env):
    lim = rl.get_limiter()
    assert lim.enabled is True
    assert lim.rps == 1.0
    assert lim.max_wait == 30.0
    assert isinstance(lim._backend, rl.MemoryBackend)


def test_factory_reads_env(clean_limiter_env):
    clean_limiter_env.setenv("TRADINGVIEW_MCP_RATE_LIMIT_RPS", "4")
    clean_limiter_env.setenv("TRADINGVIEW_MCP_RATE_LIMIT_MAX_WAIT", "5")
    lim = rl.get_limiter()
    assert lim.rps == 4.0 and lim.max_wait == 5.0
    assert rl.get_limiter() is lim  # cached singleton


def test_factory_can_disable(clean_limiter_env):
    clean_limiter_env.setenv("TRADINGVIEW_MCP_RATE_LIMIT_ENABLED", "false")
    assert rl.get_limiter().enabled is False


def test_factory_bad_float_falls_back_to_default(clean_limiter_env):
    clean_limiter_env.setenv("TRADINGVIEW_MCP_RATE_LIMIT_RPS", "not-a-number")
    assert rl.get_limiter().rps == 1.0


def test_unknown_backend_falls_back_to_memory(clean_limiter_env):
    clean_limiter_env.setenv("TRADINGVIEW_MCP_RATE_LIMIT_BACKEND", "rabbitmq")
    assert isinstance(rl.get_limiter()._backend, rl.MemoryBackend)


# ── gated_urlopen + proxy handler wiring ─────────────────────────────────────

def test_gated_urlopen_acquires_before_opening(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(rl, "acquire", lambda label=None: calls.append(("acquire", label)))
    monkeypatch.setattr(rl.urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append(("urlopen", req.host, timeout)) or "RESP")
    req = urllib.request.Request("https://data.sec.gov/x", headers={})
    assert rl.gated_urlopen(req, timeout=7) == "RESP"
    # acquire(host) must run strictly before urlopen.
    assert calls == [("acquire", "data.sec.gov"), ("urlopen", "data.sec.gov", 7)]


def test_tv_scanner_gates_on_cache_miss_only(monkeypatch):
    """ta_call must take exactly one slot on a network miss and none on a hit —
    this is the chokepoint for every tradingview_ta consumer."""
    from tradingview_mcp.core.services import tv_scanner as tv
    tv.reset_cache_for_tests()
    calls: list[str] = []
    monkeypatch.setattr(tv, "_rate_acquire", lambda label=None: calls.append(label))
    monkeypatch.setattr(tv, "_TA_AVAILABLE", True)
    monkeypatch.setattr(tv, "get_multiple_analysis",
                        lambda **_kw: {"NASDAQ:AAPL": object()})

    tv.ta_call("america", "1d", ["NASDAQ:AAPL"])   # miss → one acquire
    tv.ta_call("america", "1d", ["NASDAQ:AAPL"])   # hit  → no acquire
    assert calls == ["scanner.tradingview.com"]


def test_shed_propagates_from_gated_primary_not_fallback(monkeypatch):
    """A queue-shed (RateLimitTimeout) on the primary fetch must fail fast, not
    get caught by `except Exception` and retried on the proxy fallback."""
    from tradingview_mcp.core.services import backtest_service as bt

    def _shed(*_a, **_kw):
        raise rl.RateLimitTimeout(99.0, 30.0, "yahoo")

    monkeypatch.setattr(bt, "gated_urlopen", _shed)            # primary sheds
    # Fallback must NOT run — make it explode if it does.
    monkeypatch.setattr(bt, "build_opener_with_proxy",
                        lambda *a, **k: pytest.fail("fallback ran after a shed"),
                        raising=False)
    with pytest.raises(rl.RateLimitTimeout):
        bt._fetch_ohlcv("AAPL", "1y")


def test_shed_propagates_from_proxy_primary_not_fallback(monkeypatch):
    """Same fail-fast guarantee where the proxy opener is the primary path."""
    from tradingview_mcp.core.services import stooq_service as ss

    def _shed(*_a, **_kw):
        raise rl.RateLimitTimeout(99.0, 30.0, "stooq.com")

    monkeypatch.setattr(ss, "build_opener_with_proxy", _shed)  # primary sheds
    monkeypatch.setattr(ss, "gated_urlopen",
                        lambda *a, **k: pytest.fail("fallback ran after a shed"))
    with pytest.raises(rl.RateLimitTimeout):
        ss._fetch_csv("kgh")


def test_build_opener_attaches_rate_limit_handler(monkeypatch):
    # Empty pool → plain opener, but it must still carry the throttle handler.
    monkeypatch.delenv("PROXY_USERNAME_PREFIX", raising=False)
    monkeypatch.delenv("PROXY_PASSWORD", raising=False)
    monkeypatch.delenv("PROXY_EXTRA_URLS", raising=False)
    monkeypatch.setenv("PROXY_ENABLED", "true")
    monkeypatch.setenv("PROXY_INCLUDE_DIRECT", "false")
    opener = pm.build_opener_with_proxy()
    assert any(isinstance(h, pm._RateLimitHandler) for h in opener.handlers)
