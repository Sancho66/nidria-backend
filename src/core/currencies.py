"""ISO 4217 currency reference — from the maintained `iso4217` library, NEVER a
hand-kept list (it would rot, and per-currency decimals — guaraní 0, Tunisian
dinar 3 — are exactly what a hand list gets wrong).

A currency is REAL (exposable, usable) iff it defines a minor unit (an integer
`exponent`). That single rule excludes, BY CONSTRUCTION and self-maintaining:
precious metals XAU/XAG/XPT/XPD, test units XTS/XXX, accounting units XDR/XUA —
all carry `exponent = None` — while KEEPING the real X* currencies XOF, XAF,
XCD, XPF (which carry a real exponent). Names are English only (the library's
sole locale): the front shows code + English name, never an invented
translation. The `decimals` drive input validation AND display.
"""

from dataclasses import dataclass
from typing import Annotated

import iso4217
from pydantic import AfterValidator


@dataclass(frozen=True)
class CurrencyInfo:
    code: str
    name: str
    decimals: int


# Built once at import. `exponent is not None` = a real currency with a minor
# unit; everything else (metals, test, accounting units) is filtered out.
_SUPPORTED: dict[str, CurrencyInfo] = {
    c.code: CurrencyInfo(code=c.code, name=c.currency_name, decimals=c.exponent)
    for c in iso4217.Currency
    if c.exponent is not None
}


def is_supported(code: str) -> bool:
    """True iff `code` is an exact uppercase ISO-4217 code of a real currency.
    'eur' (lowercase), 'EURO' (not a code), 'XYZ' (unknown) → False."""
    return code in _SUPPORTED


def decimals_for(code: str) -> int:
    return _SUPPORTED[code].decimals


def list_supported() -> list[CurrencyInfo]:
    return sorted(_SUPPORTED.values(), key=lambda c: c.code)


def _validate_currency_code(value: str) -> str:
    if not is_supported(value):
        raise ValueError("Unknown ISO 4217 currency code.")
    return value


# A request-schema currency field: an exact ISO-4217 code of a real currency, or
# 422. Shared by every cost/planned-cost request (one catalogue, one validator).
CurrencyCode = Annotated[str, AfterValidator(_validate_currency_code)]


# ── Default currency at agency creation (NID-16a) ─────────────────────────────
# A fresh agency must never carry a NULL currency, else its first cost hits the
# "set the agency currency" wall (cost.currency_required). No country is
# collected at signup, so the currency is DERIVED FROM THE UI LANGUAGE — but
# only where a language maps to one obvious currency zone; every ambiguous
# language falls back to EUR. Deliberately tiny and ASSUMED — NOT a
# language→country→currency table. Reverses the earlier "no fabricated default"
# stance in favour of onboarding; an agency billing elsewhere (e.g. es → a
# Latin-American currency) changes it in Settings → Profile & brand.
DEFAULT_CURRENCY = "EUR"
_CURRENCY_BY_LANGUAGE: dict[str, str] = {
    "hu": "HUF",  # Hungarian → Hungary (unambiguous)
    "ru": "RUB",  # Russian → Russia (the dominant zone)
}


def default_currency_for_language(language: str) -> str:
    """The non-NULL currency posed on a new agency. Language → currency only
    where unambiguous (hu, ru); every other language → EUR. Always editable
    afterwards. All returned codes are real ISO-4217 currencies."""
    return _CURRENCY_BY_LANGUAGE.get(language, DEFAULT_CURRENCY)
