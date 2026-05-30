"""Tests for proxy_manager egress selection.

Covers the rotation pool (Webshare sticky sessions + fixed extra proxies +
optional direct/home slot) and how openers authenticate each kind of proxy.
Every test sets the full PROXY_* env explicitly so the ambient repo-root .env
(auto-loaded at import) can't make results non-deterministic.

Pool model: a request is assigned uniformly across a flat list of slots —
``n_sessions`` Webshare slots, then ``len(PROXY_EXTRA_URLS)`` slots, then one
direct slot if ``PROXY_INCLUDE_DIRECT``. ``random.randint(0, total-1)`` is
monkeypatched per test to pin which slot is chosen.
"""
from __future__ import annotations

import urllib.request

import pytest

from tradingview_mcp.core.services import proxy_manager as pm


@pytest.fixture
def proxy_env(monkeypatch):
    """A deterministic Webshare-only env (10 sticky sessions, no extras)."""
    monkeypatch.setenv("PROXY_HOST", "p.webshare.io")
    monkeypatch.setenv("PROXY_PORT", "80")
    monkeypatch.setenv("PROXY_USERNAME_PREFIX", "testuser")
    monkeypatch.setenv("PROXY_PASSWORD", "testpass")
    monkeypatch.setenv("PROXY_ENABLED", "true")
    monkeypatch.setenv("PROXY_SESSION_MIN", "1")
    monkeypatch.setenv("PROXY_SESSION_MAX", "10")
    monkeypatch.setenv("PROXY_INCLUDE_DIRECT", "false")
    monkeypatch.delenv("PROXY_EXTRA_URLS", raising=False)
    return monkeypatch


def _has_proxy_auth(opener: urllib.request.OpenerDirector) -> bool:
    """True iff the opener carries a ProxyBasicAuthHandler (authed proxy)."""
    return any(
        isinstance(h, urllib.request.ProxyBasicAuthHandler) for h in opener.handlers
    )


def _proxy_map(opener: urllib.request.OpenerDirector) -> dict:
    """The ProxyHandler's proxies dict (empty when going direct)."""
    for h in opener.handlers:
        if isinstance(h, urllib.request.ProxyHandler):
            return h.proxies
    return {}


# ── Empty pool → direct ───────────────────────────────────────────────────────


def test_unconfigured_means_direct(monkeypatch):
    monkeypatch.delenv("PROXY_USERNAME_PREFIX", raising=False)
    monkeypatch.delenv("PROXY_PASSWORD", raising=False)
    monkeypatch.delenv("PROXY_EXTRA_URLS", raising=False)
    monkeypatch.setenv("PROXY_ENABLED", "true")
    monkeypatch.setenv("PROXY_INCLUDE_DIRECT", "false")
    assert pm.is_proxy_configured() is False
    assert pm._select_opener_egress() is None
    assert not _has_proxy_auth(pm.build_opener_with_proxy())


def test_disabled_flag_forces_direct(proxy_env):
    proxy_env.setenv("PROXY_ENABLED", "false")
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    proxy_env.setenv("PROXY_EXTRA_URLS", "http://u:p@oracle:8888")
    # Disabled trumps every source — pool is empty.
    assert pm._select_opener_egress() is None


# ── Webshare sticky sessions ──────────────────────────────────────────────────


def test_configured_rotates_within_range(proxy_env, monkeypatch):
    # total = 10 sessions; slot index 6 → session id (min + 6) = 7.
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: 6)
    assert pm._select_opener_egress() == "http://testuser-7:testpass@p.webshare.io:80"
    assert _has_proxy_auth(pm.build_opener_with_proxy())


def test_include_direct_picks_home_on_last_slot(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    # total = 10 sessions + 1 direct = 11; the top index (10) is the home slot.
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: hi)
    assert pm._select_opener_egress() is None
    assert not _has_proxy_auth(pm.build_opener_with_proxy())


def test_include_direct_still_uses_proxy_for_session_slots(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: 3)
    assert pm._select_opener_egress() == "http://testuser-4:testpass@p.webshare.io:80"


def test_extra_handlers_survive_the_direct_slot(proxy_env, monkeypatch):
    """A cookie processor must stay attached even when the home slot is chosen
    (the Yahoo crumb bootstrap relies on this)."""
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: hi)  # direct slot
    cookie_handler = urllib.request.HTTPCookieProcessor()
    opener = pm.build_opener_with_proxy("ua", extra_handlers=(cookie_handler,))
    assert cookie_handler in opener.handlers
    assert not _has_proxy_auth(opener)


# ── Extra fixed proxies (PROXY_EXTRA_URLS) ────────────────────────────────────


def test_extra_urls_join_the_pool_after_sessions(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_SESSION_MAX", "2")  # 2 Webshare slots
    proxy_env.setenv(
        "PROXY_EXTRA_URLS",
        "http://u:p@oracle1:8888, http://u:p@oracle2:8888",  # note the space
    )
    # total = 2 sessions + 2 extras = 4; index 2 → first extra.
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: 2)
    assert pm._select_opener_egress() == "http://u:p@oracle1:8888"
    # index 3 → second extra (whitespace trimmed).
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: 3)
    assert pm._select_opener_egress() == "http://u:p@oracle2:8888"


def test_extra_only_without_webshare_creds(monkeypatch):
    for var in ("PROXY_USERNAME_PREFIX", "PROXY_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PROXY_ENABLED", "true")
    monkeypatch.setenv("PROXY_INCLUDE_DIRECT", "false")
    monkeypatch.setenv("PROXY_EXTRA_URLS", "http://u:p@oracle1:8888")
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: 0)
    # No Webshare gateway, but the extra proxy still forms a one-slot pool.
    assert pm.is_proxy_configured() is False
    assert pm._select_opener_egress() == "http://u:p@oracle1:8888"


def test_build_opener_authenticates_extra_proxy(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_SESSION_MAX", "1")  # 1 session
    proxy_env.setenv("PROXY_EXTRA_URLS", "http://node:secret@oracle1:8888")
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: 1)  # the extra slot
    opener = pm.build_opener_with_proxy()
    assert _has_proxy_auth(opener)  # creds present → auth handler attached
    assert _proxy_map(opener)["https"] == "http://node:secret@oracle1:8888"


def test_build_opener_extra_proxy_without_auth(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_SESSION_MAX", "1")
    proxy_env.setenv("PROXY_EXTRA_URLS", "http://oracle-private:8888")  # no creds
    monkeypatch.setattr(pm.random, "randint", lambda lo, hi: 1)
    opener = pm.build_opener_with_proxy()
    assert not _has_proxy_auth(opener)  # no creds → no auth handler
    assert _proxy_map(opener)["http"] == "http://oracle-private:8888"
