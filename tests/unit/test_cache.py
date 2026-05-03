"""Tests for the persistent JSON cache layer."""
from __future__ import annotations

import json
import time

import pytest

from tradingview_mcp.core.services import cache as cache_mod


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Each test gets its own cache directory and a clean in-memory store."""
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_DIR", str(tmp_path))
    cache_mod.reset_for_tests()
    yield
    cache_mod.reset_for_tests()


def test_caches_value_within_ttl():
    calls = {"n": 0}

    @cache_mod.cached(ttl_seconds=60, namespace="t1")
    def fetch(x):
        calls["n"] += 1
        return {"value": x * 2}

    assert fetch(5) == {"value": 10}
    assert fetch(5) == {"value": 10}
    assert calls["n"] == 1, "second call should hit cache"


def test_distinct_args_get_distinct_entries():
    calls = {"n": 0}

    @cache_mod.cached(ttl_seconds=60, namespace="t2")
    def fetch(x):
        calls["n"] += 1
        return {"value": x}

    fetch("AAPL")
    fetch("MSFT")
    fetch("AAPL")
    assert calls["n"] == 2


def test_ttl_expiry_re_invokes(monkeypatch):
    calls = {"n": 0}
    fake_now = [1_000_000.0]
    monkeypatch.setattr(cache_mod.time, "time", lambda: fake_now[0])

    @cache_mod.cached(ttl_seconds=10, namespace="t3")
    def fetch():
        calls["n"] += 1
        return {"v": 1}

    fetch()
    fake_now[0] += 5     # within TTL
    fetch()
    assert calls["n"] == 1
    fake_now[0] += 20    # past TTL
    fetch()
    assert calls["n"] == 2


def test_error_results_are_not_cached():
    calls = {"n": 0}

    @cache_mod.cached(ttl_seconds=60, namespace="t4")
    def fetch():
        calls["n"] += 1
        return {"error": "rate-limited", "symbol": "X"}

    fetch()
    fetch()
    assert calls["n"] == 2, "error responses must NOT be cached"


def test_persists_across_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_DIR", str(tmp_path))
    cache_mod.reset_for_tests()

    calls = {"n": 0}

    def factory():
        @cache_mod.cached(ttl_seconds=3600, namespace="persist")
        def fetch(symbol):
            calls["n"] += 1
            return {"symbol": symbol, "price": 123.45}
        return fetch

    fn1 = factory()
    fn1("AAPL")
    assert calls["n"] == 1

    # Simulate a process restart: drop in-memory state, keep file on disk.
    cache_mod.reset_for_tests()
    fn2 = factory()
    out = fn2("AAPL")
    assert out == {"symbol": "AAPL", "price": 123.45}
    assert calls["n"] == 1, "value should come from on-disk cache"


def test_clear_namespace_only():
    @cache_mod.cached(ttl_seconds=60, namespace="ns_a")
    def fa():
        return {"v": 1}

    @cache_mod.cached(ttl_seconds=60, namespace="ns_b")
    def fb():
        return {"v": 2}

    fa()
    fb()
    removed = cache_mod.clear("ns_a")
    assert removed == 1

    # ns_a re-runs, ns_b stays cached
    calls = {"a": 0, "b": 0}

    @cache_mod.cached(ttl_seconds=60, namespace="ns_a")
    def fa2():
        calls["a"] += 1
        return {"v": 1}

    @cache_mod.cached(ttl_seconds=60, namespace="ns_b")
    def fb2():
        calls["b"] += 1
        return {"v": 2}

    fa2()
    fb2()
    assert calls["a"] == 1
    assert calls["b"] == 0


def test_cache_file_is_valid_json(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_DIR", str(tmp_path))
    cache_mod.reset_for_tests()

    @cache_mod.cached(ttl_seconds=60, namespace="probe")
    def fetch(x):
        return {"x": x}

    fetch("AAPL")
    cache_file = tmp_path / "cache.json"
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert any(k.startswith("probe:") for k in data)
