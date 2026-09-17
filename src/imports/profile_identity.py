"""Identity keys shared by profile import analysis and persistence."""

import re
import unicodedata
from typing import Any

IdentityKey = tuple[str, ...]


def phone_key(value: str | None) -> str | None:
    """Remove formatting and equate 00/+ prefixes without guessing a country."""
    compact = re.sub(r"[\s().-]", "", value or "")
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    return compact if re.fullmatch(r"\+?[0-9]+", compact) else None


def name_key(first: str | None, last: str | None) -> tuple[str, str] | None:
    """Compare full names exactly after case/accent folding and outer trimming."""

    def normalize(value: str | None) -> str:
        decomposed = unicodedata.normalize("NFD", (value or "").strip().casefold())
        return "".join(c for c in decomposed if not unicodedata.combining(c))

    first_normalized, last_normalized = normalize(first), normalize(last)
    return (first_normalized, last_normalized) if first_normalized and last_normalized else None


def identity_key(person: dict[str, Any]) -> IdentityKey | None:
    """Choose one key: email, otherwise normalized phone, otherwise full name."""
    return next(iter(identity_keys(person)), None)


def identity_keys(person: dict[str, Any]) -> list[IdentityKey]:
    """Index available identities; only the incoming row chooses its lookup key."""
    keys: list[IdentityKey] = []
    if email := person.get("email"):
        keys.append(("email", email.strip().lower()))
    if phone := phone_key(person.get("phone")):
        keys.append(("phone", phone))
    if name := name_key(person.get("first_name"), person.get("last_name")):
        keys.append(("name", *name))
    return keys
