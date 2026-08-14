"""ISO 3166-1 country reference — from the maintained `pycountry` library,
NEVER a hand-kept list (same doctrine as `currencies` and its iso4217 source:
249 entries whose codes are added, retired and reassigned, and whose names in
seven languages are not ours to invent).

Two services, both born of the same report — an agency in Asunción reading
« Asunción, PY » in the conditions IT publishes to its clients:

- RENDERING (`country_name`): a legal text NAMES a country, it never prints
  a code. The name is resolved in the language of the text that carries it,
  at render time — the stored value stays the code, which is what a country
  selector, a filter and an export all want.
- VALIDATION (`normalize`): the stored code is a real one, uppercased. The
  guard lives here rather than in a screen because the API is callable by a
  third-party client, and because a code nobody can name renders as a code.

`country_name` deliberately survives an unknown value by rendering it as
written: a document may already carry a legacy code, and a rendering never
subtracts information from a legal text.
"""

import gettext
from functools import cache

import pycountry

# Built once at import: alpha-2 → the ENGLISH ISO name, which doubles as the
# gettext MESSAGE KEY of the translation catalogs below.
_ENGLISH_NAMES: dict[str, str] = {c.alpha_2: c.name for c in pycountry.countries}


def is_supported(code: str) -> bool:
    """True iff `code` is an exact uppercase ISO 3166-1 alpha-2 code.
    'fr' (lowercase), 'FRA' (alpha-3), 'ZZ' (unknown) → False."""
    return code in _ENGLISH_NAMES


def normalize(value: str | None) -> str | None:
    """The value as it must be STORED — trimmed, uppercased — or None when it
    is not a country at all. 'fr' → 'FR', ' py ' → 'PY', 'ZZ' → None."""
    if value is None:
        return None
    code = value.strip().upper()
    return code if code in _ENGLISH_NAMES else None


@cache
def _catalog(lang: str) -> gettext.NullTranslations:
    """The ISO 3166-1 name catalog of a language. `fallback=True` makes an
    absent catalog (English, whose names ARE the keys) an identity
    translation instead of an exception."""
    return gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=[lang], fallback=True)


def country_name(value: str | None, lang: str) -> str | None:
    """The country's NAME in `lang` — what a document shows in place of the
    stored code. Case-insensitive, so a legacy lowercase 'fr' renders
    « France » too. An unknown code is returned as written (never dropped:
    the text keeps what it had), an empty value stays None."""
    code = normalize(value)
    if code is None:
        return value.strip() if value and value.strip() else None
    return _catalog(lang).gettext(_ENGLISH_NAMES[code])
