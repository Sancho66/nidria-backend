"""The slug an agency gets from its name (signup 01/10). The slug is ASCII
(public URLs, the white-label ?agency= login); NFKD alone dropped every
Cyrillic or Greek letter, so « Домицилиране България » left an EMPTY slug and
the self-serve signup ended on a 422. Cyrillic and Greek are transliterated
now, and any other script gets a neutral, never-empty slug."""

import re

import pytest

from src.agencies.agencies_manager import _agency_slug, _slugify


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Neo Agence", "neo-agence"),  # Latin: unchanged
        ("Őrs Ügyvédi Iroda", "ors-ugyvedi-iroda"),  # Hungarian accents: unchanged
        ("Домицилиране България", "domitsilirane-balgariya"),  # Bulgarian
        ("Агентство Москва", "agentstvo-moskva"),  # Russian
        ("Йошкар-Ола Ёлка", "yoshkar-ola-yolka"),  # й / ё survive NFKD
        ("Київська агенція", "kiyivska-agentsiya"),  # Ukrainian
        ("Белград ђ љ", "belgrad-dj-lj"),  # Serbian
        ("Ελληνική Υπηρεσία Άλφα", "elliniki-ypiresia-alfa"),  # Greek, accents
        ("Domiciliation България 2", "domiciliation-balgariya-2"),  # mixed
    ],
)
def test_names_in_latin_cyrillic_and_greek_get_a_readable_slug(name: str, slug: str) -> None:
    assert _slugify(name) == slug
    assert _agency_slug(name) == slug


@pytest.mark.parametrize("name", ["日本の代理店", "وكالة", "🙂", "---"])
def test_other_scripts_get_a_neutral_never_empty_slug(name: str) -> None:
    assert _slugify(name) == ""
    assert re.fullmatch(r"agency-[0-9a-f]{6}", _agency_slug(name))
