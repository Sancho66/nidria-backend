"""ISO 639 language names, from the maintained `pycountry` library, NEVER a
hand-kept list (the doctrine of `countries`: the names, in seven languages,
are not ours to invent).

RENDERING only (`language_name`): a text that NAMES a language prints its
name in the language of the text, at render time, while the stored value
stays the code (what a selector, a filter and an export want). Born for the
Email module's `{{client.preferred_lang}}` (an email reads « Espagnol »,
never « es »), named in the agency's language like countries and statuses.

`language_name` survives an unknown value by rendering it as written: a
rendering never subtracts information from a text.
"""

import gettext
from functools import cache

import pycountry


@cache
def _catalog(lang: str) -> gettext.NullTranslations:
    """The ISO 639-3 name catalog of a language. `fallback=True` makes an
    absent catalog (English, whose names ARE the keys) an identity
    translation instead of an exception."""
    return gettext.translation("iso639-3", pycountry.LOCALES_DIR, languages=[lang], fallback=True)


def _english_name(code: str) -> str | None:
    """The ENGLISH ISO name of a two- or three-letter code (the gettext
    message key of the catalogs), or None when no language has it."""
    found = (
        pycountry.languages.get(alpha_2=code)
        if len(code) == 2
        else pycountry.languages.get(alpha_3=code)
        if len(code) == 3
        else None
    )
    return str(found.name) if found is not None else None


def language_name(value: str | None, lang: str) -> str | None:
    """The language's NAME in `lang`, in place of its stored code: 'es'
    in French reads « Espagnol ». Case-insensitive; an unknown code is
    returned as written (never dropped), an empty value stays None."""
    if value is None or not value.strip():
        return None
    code = value.strip()
    english = _english_name(code.lower())
    if english is None:
        return code
    return _catalog(lang).gettext(english)
