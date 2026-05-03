"""Tests for BETA ETF NAV scraper (no network — pure parser tests)."""
from __future__ import annotations

import pytest

from tradingview_mcp.core.services import beta_etf_scraper as bes
from tradingview_mcp.core.services import cache as cache_mod


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_DIR", str(tmp_path))
    cache_mod.reset_for_tests()
    yield
    cache_mod.reset_for_tests()


_FIXTURE_HTML = """
<html><body>
<th><strong><span>Ticker</span></strong></th>
<td style="width: 50%;height: 23px">ETFBW20TR</td>
<th><strong><span>ISIN</span></strong></th>
<td style="width: 50%;height: 23px">PLBTETF00015</td>

<table><tbody>
<tr><th>Skorygowana Wartość Aktywów Netto (SWAN)</th>
<td style="width: 50%">300 435 898,00 zł</td></tr>
</tbody></table>

<table><tbody>
<tr class="h-24"><td>2026-04-29</td><td>69.74</td><td>4 278 760,00</td></tr>
<tr class="h-24"><td>2026-04-28</td><td>69.72</td><td>4 268 760,00</td></tr>
<tr class="h-24"><td>2026-04-27</td><td>70.26</td><td>4 258 760,00</td></tr>
</tbody></table>
</body></html>
"""


def test_parse_etf_page_extracts_all_fields():
    out = bes._parse_etf_page(_FIXTURE_HTML)
    assert out["ticker"] == "ETFBW20TR"
    assert out["isin"] == "PLBTETF00015"
    assert out["nav"] == 69.74
    assert out["nav_date"] == "2026-04-29"
    assert out["certificates_outstanding"] == 4_278_760.0
    assert out["assets_pln"] == 300_435_898.0


def test_parse_pl_number_handles_pl_format():
    assert bes._parse_pl_number("4 278 760,00") == 4_278_760.0
    assert bes._parse_pl_number("69.74") == 69.74
    assert bes._parse_pl_number("123,45") == 123.45
    assert bes._parse_pl_number("1.234,56") == 1234.56  # PL: '.' = thousand sep
    assert bes._parse_pl_number("not a number") is None


def test_parse_etf_page_picks_first_row_as_latest():
    """First <tr class='h-24'> is the most recent date."""
    out = bes._parse_etf_page(_FIXTURE_HTML)
    assert out["nav_date"] == "2026-04-29"
    assert out["nav"] == 69.74


def test_parse_etf_page_returns_empty_on_garbage():
    assert bes._parse_etf_page("<html>nothing here</html>") == {}


def test_get_etf_nav_unknown_ticker_returns_error(monkeypatch):
    """Unknown tickers must NOT hit the network."""
    def boom(*a, **kw):
        raise AssertionError("network should not be called for unknown ticker")
    monkeypatch.setattr(bes, "_fetch", boom)

    result = bes.get_etf_nav("UNKNOWN_TICKER")
    assert "error" in result
    assert result["ticker"] == "UNKNOWN_TICKER"
    assert "unknown BETA ETF ticker" in result["error"]


def test_get_etf_nav_success(monkeypatch):
    monkeypatch.setattr(bes, "_fetch", lambda url: _FIXTURE_HTML)

    result = bes.get_etf_nav("ETFBW20TR")
    assert result["ticker"] == "ETFBW20TR"
    assert result["nav"] == 69.74
    assert result["nav_date"] == "2026-04-29"
    assert result["assets_pln"] == 300_435_898.0
    assert result["source"] == "agiofunds.pl"
    assert "url" in result
    assert "?confirm=true" in result["url"]


def test_get_etf_nav_handles_fetch_failure(monkeypatch):
    def fail(url):
        raise ConnectionError("upstream down")
    monkeypatch.setattr(bes, "_fetch", fail)

    result = bes.get_etf_nav("ETFBW20TR")
    assert "error" in result
    assert "ConnectionError" in result["error"]
    assert result["source"] == "agiofunds.pl"


def test_get_etf_nav_handles_layout_change(monkeypatch):
    """If the table layout changes, return error rather than partial data."""
    monkeypatch.setattr(bes, "_fetch", lambda url: "<html><body>no NAV here</body></html>")

    result = bes.get_etf_nav("ETFBW20TR")
    assert "error" in result
    assert "NAV row not found" in result["error"]


def test_supported_tickers_includes_priority_funds():
    """The CLAUDE.md priority set must remain supported."""
    supported = bes.supported_tickers()
    assert "ETFBW20TR" in supported
    assert "ETFBS80TR" in supported
    # All slugs must be non-empty
    for ticker in supported:
        assert bes._TICKER_TO_SLUG[ticker]
