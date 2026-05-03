"""GPW ticker → search aliases for Polish news/RSS filtering.

Polish news outlets often refer to companies by full or partial name rather
than by their 3-letter GPW exchange code. ``KGH`` rarely appears in headlines
about KGHM Polska Miedź — but ``KGHM`` and ``Polska Miedź`` do.

Each entry maps a TradingView/GPW ticker to a list of search aliases (names
*and* common ticker variants). Match is substring, case-insensitive.

Used by news_service when a Polish ticker is filtered against pl_stocks RSS
feeds (Bankier, Money.pl, Comparic).

Coverage: WIG20 + portfolio-relevant smaller caps. Easy to extend.
"""
from __future__ import annotations

GPW_COMPANY_NAMES: dict[str, list[str]] = {
    # WIG20 / mWIG40 — large caps
    "KGH":   ["KGHM", "KGH", "Polska Miedź"],
    "CDR":   ["CD Projekt", "CDPROJEKT", "CDR"],
    "JSW":   ["JSW", "Jastrzębska Spółka Węglowa", "Jastrzebska"],
    "PKN":   ["Orlen", "PKN", "PKN Orlen"],
    "PZU":   ["PZU", "Powszechny Zakład Ubezpieczeń"],
    "PEO":   ["Pekao", "PEO", "Bank Pekao"],
    "PKO":   ["PKO", "PKO BP", "PKO Bank Polski"],
    "DNP":   ["Dino", "DNP", "Dino Polska"],
    "ALR":   ["Alior", "ALR", "Alior Bank"],
    "LPP":   ["LPP"],
    "ALE":   ["Allegro", "ALE"],
    "OPL":   ["Orange", "OPL", "Orange Polska"],
    "SPL":   ["Santander", "SPL", "Santander Bank"],
    "MBK":   ["mBank", "MBK"],
    "ASE":   ["Asseco", "ASE", "Asseco Poland"],
    "TPE":   ["Tauron", "TPE"],
    "PGE":   ["PGE"],
    "CCC":   ["CCC"],
    "KTY":   ["Kęty", "KTY"],
    "KRU":   ["Kruk", "KRU"],
    "CPS":   ["Cyfrowy Polsat", "CPS", "Polsat"],
    "EUR":   ["Eurocash", "EUR"],
    "BDX":   ["Budimex", "BDX"],
    "ATT":   ["Grupa Azoty", "ATT"],
    # Portfolio-relevant smaller caps from user
    "CRI":   ["Creotech", "CRI", "Creotech Instruments"],
    "CRQ":   ["Creotech Quantum", "CRQ", "CRQUANTUM"],
    # BETA ETF-y — niska szansa pojawienia się w prasowych newsach,
    # ale dorzucamy bo użytkownik je trzyma
    "ETFBW20TR":  ["Beta ETF WIG20TR", "ETFBW20TR", "BETAW20TR"],
    "ETFBCASH":   ["Beta ETF WIGtech", "ETFBCASH", "BETACASH", "BCASH"],
    "ETFBS80TR":  ["Beta ETF sWIG80TR", "ETFBS80TR", "BETAS80TR"],
}


def search_aliases(ticker: str) -> list[str]:
    """Return search aliases for *ticker*. Falls back to [ticker] if unknown."""
    t = ticker.strip().upper()
    return GPW_COMPANY_NAMES.get(t, [t])
