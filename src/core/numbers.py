"""Whole numbers written the way each interface language writes them, for a
text that prints a count (the Email module's `{{company.employee_count}}`).

Babel is not a dependency and none is added: the grouping of the seven
interface languages is copied from ICU (CLDR), read on 08/10/2026 with
Node 22.22.2 (ICU 78.2, CLDR 48.0):

    node -e "for (const l of ['fr','en','es','ru','pt-PT','it','hu'])
      console.log(l, JSON.stringify([1234,12345,1234567].map(
        n => new Intl.NumberFormat(l).format(n))))"

    fr     1 234 / 12 345 / 1 234 567   (U+202F, narrow no-break space)
    en     1,234 / 12,345 / 1,234,567
    es     1234 / 12.345 / 1.234.567     (no grouping under five digits)
    ru     1 234 / 12 345 / 1 234 567   (U+00A0, no-break space)
    pt-PT  1234 / 12 345 / 1 234 567     (U+00A0; no grouping under five digits)
    it     1234 / 12.345 / 1.234.567     (no grouping under five digits)
    hu     1234 / 12 345 / 1 234 567     (U+00A0; no grouping under five digits)

A negative number takes "-" (HYPHEN-MINUS) in all seven (-1234567, same
source). The tests check these exact strings.
"""

#: Language -> (grouping separator, CLDR minimumGroupingDigits). Groups are
#: of three digits in all seven; a number is grouped once it has at least
#: 3 + minimumGroupingDigits digits.
_GROUPING: dict[str, tuple[str, int]] = {
    "fr": (" ", 1),
    "en": (",", 1),
    "es": (".", 2),
    "ru": (" ", 1),
    "pt": (" ", 2),
    "it": (".", 2),
    "hu": (" ", 2),
}


def format_integer(value: int, lang: str) -> str:
    """`value` with the digit grouping of `lang` (one of the seven interface
    languages; any other reads as English)."""
    separator, minimum = _GROUPING.get(lang, _GROUPING["en"])
    digits = str(abs(value))
    if len(digits) >= 3 + minimum:
        head = len(digits) % 3 or 3
        groups = [digits[:head]] + [digits[i : i + 3] for i in range(head, len(digits), 3)]
        digits = separator.join(groups)
    return f"-{digits}" if value < 0 else digits
