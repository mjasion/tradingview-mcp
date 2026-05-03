"""Tests for SEC EDGAR insider-transactions service (no network)."""
from __future__ import annotations

import pytest

from tradingview_mcp.core.services import cache as cache_mod
from tradingview_mcp.core.services import sec_service


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_DIR", str(tmp_path))
    cache_mod.reset_for_tests()
    yield
    cache_mod.reset_for_tests()


def _fake_tickers() -> dict:
    """Shape of /files/company_tickers.json (numeric-string keys)."""
    return {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    }


def _fake_submissions(form_seq: list[str]) -> dict:
    n = len(form_seq)
    return {
        "filings": {
            "recent": {
                "form":             form_seq,
                "filingDate":       [f"2026-04-{i+1:02d}" for i in range(n)],
                "accessionNumber":  [f"0000320193-26-{i:06d}" for i in range(n)],
                "primaryDocument":  ["form4.xml" if f == "4" else "doc.htm" for f in form_seq],
            }
        }
    }


def test_lookup_unknown_ticker_returns_error(monkeypatch):
    monkeypatch.setattr(sec_service, "_http_json", lambda url: _fake_tickers())
    out = sec_service.get_insider_transactions("ZZZZ")
    assert out["error"]
    assert "ZZZZ" in out["error"]


def test_returns_only_form_4_filings(monkeypatch):
    forms = ["10-Q", "8-K", "4", "4", "SCHEDULE 13G", "4"]
    submissions = _fake_submissions(forms)

    def fake_http(url):
        if "company_tickers" in url:
            return _fake_tickers()
        if "submissions" in url:
            return submissions
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(sec_service, "_http_json", fake_http)

    out = sec_service.get_insider_transactions("AAPL", limit=10)
    assert "error" not in out
    assert out["cik"] == 320193
    assert out["name"] == "Apple Inc."
    assert out["count"] == 3            # three "4" forms in the list
    assert len(out["filings"]) == 3
    for f in out["filings"]:
        assert f["url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")
        assert "form4.xml" in f["url"]


def test_limit_caps_filings_returned(monkeypatch):
    forms = ["4"] * 25
    submissions = _fake_submissions(forms)

    def fake_http(url):
        if "company_tickers" in url:
            return _fake_tickers()
        return submissions

    monkeypatch.setattr(sec_service, "_http_json", fake_http)

    out = sec_service.get_insider_transactions("AAPL", limit=5)
    assert len(out["filings"]) == 5
    assert out["count"] == 25           # total still reflects full window


def test_network_failure_returns_error_dict(monkeypatch):
    def fake_http(url):
        if "company_tickers" in url:
            return _fake_tickers()
        raise OSError("simulated DNS failure")

    monkeypatch.setattr(sec_service, "_http_json", fake_http)

    out = sec_service.get_insider_transactions("AAPL")
    assert "error" in out
    assert "OSError" in out["error"]
    assert out["cik"] == 320193          # ticker map still resolved


def test_cik_is_zero_padded_in_url(monkeypatch):
    """SEC EDGAR requires the CIK to be zero-padded to 10 digits."""
    captured = {}

    def fake_http(url):
        captured.setdefault("urls", []).append(url)
        if "company_tickers" in url:
            return _fake_tickers()
        return _fake_submissions(["4"])

    monkeypatch.setattr(sec_service, "_http_json", fake_http)
    sec_service.get_insider_transactions("AAPL")

    submission_url = next(u for u in captured["urls"] if "submissions" in u)
    assert "CIK0000320193.json" in submission_url
