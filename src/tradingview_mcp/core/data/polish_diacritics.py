"""ASCII slug → Polish-diacritic word map for PAP scraper.

PAP article slugs strip diacritics (jastrzebska → jastrzębska, slask → śląsk).
We restore diacritics for the most common headline tokens so that
``_slug_to_title`` produces readable Polish.

Coverage: ~50 high-frequency tokens for finance/markets/companies. Words
not in the map are returned as-is (acceptable ASCII fallback).
"""
from __future__ import annotations

# Lowercase ASCII slug-token → lowercase Polish form.
# Capitalisation is restored by the caller (first-letter only — Polish
# headlines don't title-case every word like English).
SLUG_DIACRITICS: dict[str, str] = {
    # Geo
    "slask": "śląsk",
    "slasku": "śląsku",
    "slaski": "śląski",
    "slaska": "śląska",
    "lodz": "łódź",
    "lodzi": "łodzi",
    "krakow": "kraków",
    "krakowa": "krakowa",
    "krakowie": "krakowie",
    "gdansk": "gdańsk",
    "gdanska": "gdańska",
    "gdyni": "gdyni",
    "poznan": "poznań",
    "poznania": "poznania",
    "wroclaw": "wrocław",
    "wroclawia": "wrocławia",
    "polska": "polska",
    "polsce": "polsce",
    "polski": "polski",
    "polskie": "polskie",
    "polskich": "polskich",

    # Finance / markets
    "spolka": "spółka",
    "spolki": "spółki",
    "spolek": "spółek",
    "wegiel": "węgiel",
    "wegla": "węgla",
    "miedz": "miedź",
    "miedzi": "miedzi",
    "rynek": "rynek",
    "rynku": "rynku",
    "wyniki": "wyniki",
    "akcje": "akcje",
    "zysk": "zysk",
    "zysku": "zysku",
    "strata": "strata",
    "straty": "straty",
    "wzrost": "wzrost",
    "wzrostu": "wzrostu",
    "spadek": "spadek",
    "obligacje": "obligacje",
    "panstwo": "państwo",
    "panstwowy": "państwowy",
    "panstwowa": "państwowa",
    "banku": "banku",
    "rzad": "rząd",
    "rzadu": "rządu",

    # Verbs / common words
    "bedzie": "będzie",
    "beda": "będą",
    "moze": "może",
    "moga": "mogą",
    "wzrosnie": "wzrośnie",
    "rosnac": "rosnąć",
    "rozwoj": "rozwój",
    "zmiana": "zmiana",
    "zmiany": "zmiany",
    "powiedzial": "powiedział",
    "powiedziala": "powiedziała",
    "ujrza": "ujrzą",

    # Frequent surnames / orgs
    "jastrzebska": "jastrzębska",
    "spoldzielnia": "spółdzielnia",
}


def restore_diacritics(token: str) -> str:
    """Return Polish-diacritic form for *token* if known, else *token* unchanged.

    Matching is case-insensitive on the slug; the returned form is lowercase
    (callers handle capitalisation).
    """
    return SLUG_DIACRITICS.get(token.lower(), token)
