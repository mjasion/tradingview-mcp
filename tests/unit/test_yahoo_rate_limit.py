"""Tests for Yahoo Finance 429 handling — retry, cooldown, structured error.

The shape of these tests mirrors the user-observed failure mode: sequential
``next_earnings`` / ``dividend_history`` calls all 429 because Yahoo
rate-limits per IP. The goal is to verify that:

* The first 429 triggers exactly one retry (after honoring Retry-After).
* A second 429 raises :class:`YahooRateLimited` carrying the delay.
* ``get_earnings`` / ``get_dividends`` translate that into a structured
  ``upstream_status: "rate_limited"`` envelope (no opaque HTTPError string).
* The process-wide cooldown gate causes a second caller to wait — so a
  parallel scan doesn't burn requests during the active rate-limit window.
"""
from __future__ import annotations

import threading
import time
import urllib.error

import pytest

from tradingview_mcp.core.services import yahoo_finance_service as yfs


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Each test starts with a fresh cooldown and a stubbed crumb."""
    yfs._CRUMB_CACHE["crumb"] = "test-crumb"
    yfs._CRUMB_CACHE["cookies"] = "test=1"
    with yfs._RATE_LIMIT_LOCK:
        yfs._RATE_LIMIT_COOLDOWN_UNTIL = 0.0
    # Don't actually sleep during retries — the timing logic is verified
    # separately via the cooldown gate.
    monkeypatch.setattr(yfs.time, "sleep", lambda _s: None)
    yield
    with yfs._RATE_LIMIT_LOCK:
        yfs._RATE_LIMIT_COOLDOWN_UNTIL = 0.0


def _make_429(retry_after: str | None = "12") -> urllib.error.HTTPError:
    """Construct an HTTPError as urllib would raise on a 429 response."""
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError(
        url="https://query1.finance.yahoo.com/v10/finance/quoteSummary/X",
        code=429,
        msg="Too Many Requests",
        hdrs=headers,  # type: ignore[arg-type]
        fp=None,
    )


class _FakeOpener:
    """Stand-in for ``build_opener_with_proxy()`` — yields a fixed sequence."""

    def __init__(self, responder):
        self._responder = responder
        self.addheaders = []

    def open(self, req, timeout=None):  # noqa: ARG002
        return self._responder(req)


def _patch_opener(monkeypatch, responder):
    """Make every quoteSummary opener.open() call go through ``responder``."""
    monkeypatch.setattr(
        yfs, "build_opener_with_proxy",
        lambda *a, **kw: _FakeOpener(responder),
    )


# ── Retry-After parsing ───────────────────────────────────────────────────────


def test_parse_retry_after_uses_header_when_numeric():
    assert yfs._parse_retry_after("15") == 15.0


def test_parse_retry_after_caps_unreasonable_values():
    # Yahoo occasionally sends ludicrous values — clamp to 60s.
    assert yfs._parse_retry_after("3600") == 60.0


def test_parse_retry_after_falls_back_when_missing():
    assert yfs._parse_retry_after(None, default=7.0) == 7.0


def test_parse_retry_after_falls_back_when_garbage():
    assert yfs._parse_retry_after("soon", default=5.0) == 5.0


# ── Cooldown gate ─────────────────────────────────────────────────────────────


def test_cooldown_blocks_until_window_expires(monkeypatch):
    """A second caller must wait for the cooldown set by the first 429."""
    # Don't no-op sleep here — we want to count how many seconds of wait
    # the gate produces.
    slept: list[float] = []
    monkeypatch.setattr(yfs.time, "sleep", lambda s: slept.append(s))

    yfs._record_rate_limit(2.5)
    yfs._wait_for_rate_limit_window()
    # The loop sleeps in <=1s slices until the window is past. With a 2.5s
    # cooldown and stubbed sleep, we expect at least one slice was issued.
    assert slept, "cooldown gate should have invoked sleep at least once"
    assert sum(slept) >= 2.0


def test_cooldown_no_wait_when_window_already_past(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(yfs.time, "sleep", lambda s: slept.append(s))
    # No cooldown set → wait_for must be a no-op.
    yfs._wait_for_rate_limit_window()
    assert slept == []


def test_record_rate_limit_keeps_longest_window():
    yfs._record_rate_limit(2.0)
    first = yfs._RATE_LIMIT_COOLDOWN_UNTIL
    yfs._record_rate_limit(1.0)  # shorter — must not shrink the window
    assert yfs._RATE_LIMIT_COOLDOWN_UNTIL == first


# ── Single retry behavior ─────────────────────────────────────────────────────


def test_first_429_retries_then_succeeds(monkeypatch):
    """One 429, then a successful response — caller sees the success."""
    calls = []

    def responder(req):  # noqa: ARG001
        calls.append(1)
        if len(calls) == 1:
            raise _make_429("3")
        # Second call succeeds — return a minimal quoteSummary payload.
        class _Resp:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def read(self_):
                return b'{"quoteSummary": {"result": [{"calendarEvents": {}}]}}'
        return _Resp()

    _patch_opener(monkeypatch, responder)
    result = yfs._fetch_quote_summary("AAPL", ["calendarEvents"])
    assert "calendarEvents" in result
    assert len(calls) == 2  # exactly one retry


def test_two_consecutive_429s_raise_yahoo_rate_limited(monkeypatch):
    calls = []

    def responder(req):  # noqa: ARG001
        calls.append(1)
        raise _make_429("10")

    _patch_opener(monkeypatch, responder)
    with pytest.raises(yfs.YahooRateLimited) as ei:
        yfs._fetch_quote_summary("AAPL", ["calendarEvents"])
    assert ei.value.retry_after_seconds >= 10
    assert len(calls) == 2  # no third attempt


def test_yahoo_rate_limited_records_cooldown_for_other_callers(monkeypatch):
    """When a 429 happens, the cooldown must be set for the whole process."""
    def responder(req):  # noqa: ARG001
        raise _make_429("8")

    _patch_opener(monkeypatch, responder)
    with pytest.raises(yfs.YahooRateLimited):
        yfs._fetch_quote_summary("AAPL", ["calendarEvents"])

    # Cooldown should now be in the future — the second 429 retry would have
    # extended it via _record_rate_limit.
    remaining = yfs._RATE_LIMIT_COOLDOWN_UNTIL - time.monotonic()
    assert remaining > 0


# ── Structured response from get_earnings / get_dividends ─────────────────────


def test_get_earnings_translates_rate_limit_to_structured_response(monkeypatch):
    # Disable disk-cache lookup so the @cached wrapper actually invokes us.
    monkeypatch.setattr(yfs, "_fetch_quote_summary",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            yfs.YahooRateLimited(retry_after_seconds=15, detail="boom")))

    # The @cached decorator caches by symbol; use a unique symbol so we don't
    # collide with any other test's cache entry.
    out = yfs.get_earnings("RATELIMITTEST1")
    assert out["error"] == "yahoo_rate_limited"
    assert out["upstream_status"] == "rate_limited"
    assert out["retry_after_seconds"] == 15
    assert "retry" in out["retry_hint"].lower()
    # Importantly: NO opaque HTTPError 429 string.
    assert "HTTPError" not in out["retry_hint"]


def test_get_dividends_translates_rate_limit_to_structured_response(monkeypatch):
    monkeypatch.setattr(yfs, "_fetch_quote_summary",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            yfs.YahooRateLimited(retry_after_seconds=20)))
    out = yfs.get_dividends("RATELIMITTEST2")
    assert out["upstream_status"] == "rate_limited"
    assert out["retry_after_seconds"] == 20


def test_rate_limited_response_not_cached(monkeypatch):
    """A rate-limited result must not poison the 6h earnings cache."""
    raise_count = [0]

    def boom(*a, **kw):
        raise_count[0] += 1
        raise yfs.YahooRateLimited(retry_after_seconds=10)

    monkeypatch.setattr(yfs, "_fetch_quote_summary", boom)

    # Use a unique symbol per call to avoid collision with earlier tests.
    yfs.get_earnings("RATELIMITNOCACHE")
    yfs.get_earnings("RATELIMITNOCACHE")
    # If errors were cached, the second call wouldn't hit _fetch_quote_summary.
    assert raise_count[0] == 2


# ── Sequential semaphore (no parallelism on quoteSummary) ─────────────────────


def test_quote_summary_semaphore_is_strictly_sequential(monkeypatch):
    """Two threads hitting quoteSummary at once must serialize through the
    BoundedSemaphore(1). We assert this by observing that at no point are
    both inside the critical section simultaneously."""
    in_flight = []
    max_seen = []
    barrier = threading.Lock()

    def responder(req):  # noqa: ARG001
        with barrier:
            in_flight.append(1)
            max_seen.append(len(in_flight))
        time.sleep(0.01)  # simulate a tiny network call
        with barrier:
            in_flight.pop()

        class _Resp:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def read(self_):
                return b'{"quoteSummary": {"result": [{"calendarEvents": {}}]}}'
        return _Resp()

    _patch_opener(monkeypatch, responder)

    # Don't no-op the real time.sleep inside the worker — we need it.
    # The autouse fixture stubs yfs.time.sleep (used for backoff). The
    # workers above use the imported stdlib time.sleep, which is unaffected.

    def worker():
        yfs._fetch_quote_summary("AAPL", ["calendarEvents"])

    ts = [threading.Thread(target=worker) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)
    assert max(max_seen) == 1, f"semaphore allowed {max(max_seen)} concurrent calls"
