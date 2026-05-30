"""Tests for proxy_manager egress selection.

Covers the rotation logic and the optional ``PROXY_INCLUDE_DIRECT`` slot that
folds the host's own (home) IP into the rotation alongside the proxy sticky
sessions. Every test sets the full PROXY_* env explicitly so the ambient
repo-root .env (auto-loaded at import) can't make results non-deterministic.
"""
from __future__ import annotations

import urllib.request

import pytest

from tradingview_mcp.core.services import proxy_manager as pm


@pytest.fixture
def proxy_env(monkeypatch):
    """A deterministic, fully-configured proxy env (10 sticky sessions)."""
    monkeypatch.setenv("PROXY_HOST", "p.webshare.io")
    monkeypatch.setenv("PROXY_PORT", "80")
    monkeypatch.setenv("PROXY_USERNAME_PREFIX", "testuser")
    monkeypatch.setenv("PROXY_PASSWORD", "testpass")
    monkeypatch.setenv("PROXY_ENABLED", "true")
    monkeypatch.setenv("PROXY_SESSION_MIN", "1")
    monkeypatch.setenv("PROXY_SESSION_MAX", "10")
    monkeypatch.setenv("PROXY_INCLUDE_DIRECT", "false")
    return monkeypatch


def _has_proxy(opener: urllib.request.OpenerDirector) -> bool:
    """True iff the opener carries our ProxyBasicAuthHandler (proxy egress)."""
    return any(
        isinstance(h, urllib.request.ProxyBasicAuthHandler) for h in opener.handlers
    )


def test_unconfigured_means_direct(monkeypatch):
    monkeypatch.delenv("PROXY_USERNAME_PREFIX", raising=False)
    monkeypatch.delenv("PROXY_PASSWORD", raising=False)
    monkeypatch.setenv("PROXY_ENABLED", "true")
    assert pm.is_proxy_configured() is False
    assert pm._select_opener_egress() is None
    assert not _has_proxy(pm.build_opener_with_proxy())


def test_disabled_flag_forces_direct(proxy_env):
    proxy_env.setenv("PROXY_ENABLED", "false")
    assert pm.is_proxy_configured() is False
    assert pm._select_opener_egress() is None


def test_configured_rotates_within_range(proxy_env, monkeypatch):
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: 7)
    assert pm._select_opener_egress() == "http://testuser-7:testpass@p.webshare.io:80"
    assert _has_proxy(pm.build_opener_with_proxy())


def test_include_direct_picks_home_on_overflow_slot(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    # With include_direct, the picker rolls randint(lo, hi+1); the extra slot
    # (> hi) means "go direct / home". Return the top of the passed range.
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: hi)
    assert pm._select_opener_egress() is None
    assert not _has_proxy(pm.build_opener_with_proxy())


def test_include_direct_still_uses_proxy_for_session_slots(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: 3)
    assert pm._select_opener_egress() == "http://testuser-3:testpass@p.webshare.io:80"
    assert _has_proxy(pm.build_opener_with_proxy())


def test_extra_handlers_survive_the_direct_slot(proxy_env, monkeypatch):
    """A cookie processor must stay attached even when the home slot is chosen
    (the Yahoo crumb bootstrap relies on this)."""
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: hi)  # direct slot
    cookie_handler = urllib.request.HTTPCookieProcessor()
    opener = pm.build_opener_with_proxy("ua", extra_handlers=(cookie_handler,))
    assert cookie_handler in opener.handlers
    assert not _has_proxy(opener)
