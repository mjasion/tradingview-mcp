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

import io
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


# ── format_proxy_report (CLI presentation) ────────────────────────────────────


def test_format_report_disabled_shows_note():
    out = pm.format_proxy_report({"enabled": False, "note": "PROXY_ENABLED is not 'true'."})
    assert out == "PROXY_ENABLED is not 'true'."


def test_format_report_lists_each_egress_with_status():
    result = {
        "enabled": True,
        "ok_count": 1,
        "total": 2,
        "egresses": [
            {"ok": True, "ip": "1.2.3.4", "country": "PL", "city": "Warsaw",
             "org": "AS8819 Metro Internet", "error": None, "label": "direct/home"},
            {"ok": False, "ip": None, "country": None, "city": None,
             "org": None, "error": "boom", "label": "webshare#1"},
        ],
    }
    out = pm.format_proxy_report(result)
    assert "1/2 OK" in out
    assert "✓ direct/home" in out
    assert "1.2.3.4" in out and "PL Warsaw" in out
    assert "AS8819 Metro Internet" in out  # organization surfaced
    assert "✗ webshare#1" in out
    assert "error: boom" in out


def test_format_report_surfaces_note_for_session_cap():
    result = {
        "enabled": True, "ok_count": 1, "total": 1,
        "egresses": [{"ok": True, "ip": "9.9.9.9", "country": None, "city": None,
                      "error": None, "label": "webshare#1"}],
        "note": "webshare sessions 17..250 not probed (cap)",
    }
    out = pm.format_proxy_report(result)
    assert "note: webshare sessions 17..250 not probed (cap)" in out


def test_format_report_empty_pool_shows_note():
    out = pm.format_proxy_report(
        {"enabled": True, "ok_count": 0, "total": 0, "egresses": [],
         "note": "No egresses configured."}
    )
    assert "0/0 OK" in out
    assert "No egresses configured." in out


# ── build_egress_jobs (the rotation pool, as an ordered work-list) ────────────


def test_build_egress_jobs_orders_the_whole_pool(proxy_env):
    proxy_env.setenv("PROXY_SESSION_MAX", "3")
    proxy_env.setenv("PROXY_EXTRA_URLS", "http://u:p@oracle1:8888")
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    jobs, note = pm.build_egress_jobs()
    assert [label for label, _ in jobs] == [
        "webshare#1", "webshare#2", "webshare#3", "extra:oracle1:8888", "direct/home",
    ]
    assert jobs[-1][1] is None  # direct slot carries a None url
    assert note is None


def test_build_egress_jobs_caps_webshare_and_notes_the_rest(proxy_env):
    proxy_env.setenv("PROXY_SESSION_MAX", "50")
    jobs, note = pm.build_egress_jobs()
    assert len(jobs) == pm._CHECK_PROXY_SESSION_CAP  # 16
    assert note == "webshare sessions 17..50 not probed (cap)"


def test_build_egress_jobs_empty_when_disabled(proxy_env):
    proxy_env.setenv("PROXY_ENABLED", "false")
    assert pm.build_egress_jobs() == ([], None)


# ── check_proxy / run_check_proxy (probe orchestration, no network) ───────────


def _fake_probe(url, timeout=8):
    """Deterministic stand-in for _probe_egress — no sockets, dead LAN proxy."""
    if url is None:
        return {"ok": True, "ip": "83.1.1.1", "country": "PL", "city": "Warsaw",
                "org": "AS8819 Metro Internet", "error": None}
    if "oracle-dead" in url:
        return {"ok": False, "ip": None, "country": None, "city": None,
                "org": None, "error": "No route to host"}
    return {"ok": True, "ip": "64.0.0.1", "country": "DE", "city": "Frankfurt",
            "org": "AS212238 Datacamp", "error": None}


def test_check_proxy_probes_every_egress_in_order(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_SESSION_MAX", "2")
    proxy_env.setenv("PROXY_EXTRA_URLS", "http://u:p@oracle-dead:30888")
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    monkeypatch.setattr(pm, "_probe_egress", _fake_probe)
    res = pm.check_proxy()
    assert res["total"] == 4 and res["ok_count"] == 3  # 2 webshare + 1 dead extra + direct
    # Order preserved despite concurrent probing.
    assert [e["label"] for e in res["egresses"]] == [
        "webshare#1", "webshare#2", "extra:oracle-dead:30888", "direct/home",
    ]
    dead = next(e for e in res["egresses"] if e["label"].startswith("extra:"))
    assert dead["ok"] is False and "No route to host" in dead["error"]


def test_run_check_proxy_non_tty_streams_each_and_summarizes(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_SESSION_MAX", "1")
    proxy_env.setenv("PROXY_INCLUDE_DIRECT", "true")
    monkeypatch.setattr(pm, "_probe_egress", _fake_probe)
    buf = io.StringIO()
    res = pm.run_check_proxy(buf, is_tty=False)
    out = buf.getvalue()
    assert res["ok_count"] == 2 and res["total"] == 2
    # Per-egress progress lines with the running [k/N] counter…
    assert "[1/2]" in out and "[2/2]" in out
    assert "webshare#1" in out and "direct/home" in out
    assert "AS8819 Metro Internet" in out  # organization shown
    # …a final summary…
    assert "2/2 OK" in out
    # …and NO terminal control codes when not a tty (clean logs / -T exec).
    assert "\x1b[" not in out


def test_run_check_proxy_disabled_reports_and_returns(monkeypatch):
    monkeypatch.setenv("PROXY_ENABLED", "false")
    buf = io.StringIO()
    res = pm.run_check_proxy(buf, is_tty=False)
    assert res["enabled"] is False
    assert "PROXY_ENABLED" in buf.getvalue()


def test_run_check_proxy_flags_session_cap_note(proxy_env, monkeypatch):
    proxy_env.setenv("PROXY_SESSION_MAX", "50")
    monkeypatch.setattr(pm, "_probe_egress", _fake_probe)
    buf = io.StringIO()
    res = pm.run_check_proxy(buf, is_tty=False)
    assert res["total"] == pm._CHECK_PROXY_SESSION_CAP
    assert "note: webshare sessions 17..50 not probed (cap)" in buf.getvalue()
