"""The dynamic tokens of the client documents (lot 13/08, jetons découvrables).

ONE token existed before this lot — `{agency_name}`, resolved at READ time
in `consents_manager._resolve`. Nothing named it anywhere: an agency writing
its own conditions could not know a token existed, and the front carried a
hand-kept mirror of the list. Both are closed here — the CATALOGUE below is
the single source, served with a human label and the agency's CURRENT value.

THREE RULES, in order of importance:

1. A KNOWN token resolves to its current value. Empty profile field → empty
   string, never a bracket, never internal markup: the published text is what
   a client reads, and the client is never shown our plumbing (same doctrine
   as the omission-by-segment lot, which took the brackets out).
2. An UNKNOWN token is left VERBATIM. Never an exception, never a blank — a
   typo must not be able to mutilate a published legal text. It is SIGNALLED
   at edition (`unknown_tokens`, served on the agency payload), where the
   agency can still fix it.
3. Resolution happens at READ time; the version hash covers the RAW text.
   Filling a profile field therefore changes what clients read WITHOUT
   publishing a version and WITHOUT re-gating them (already true of
   `{agency_name}`). The token is the agency's own fact, not a clause.

One token per PROFILE FIELD, no derived ones: the rule is explainable in a
sentence («chaque champ de votre profil est un jeton»), and the agency that
uses them never has to touch its text again when its profile changes."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from shared.models.agency import Agency

# What a token LOOKS like when we hunt one to SIGNAL: anything brace-wrapped
# on a single line. Deliberately WIDER than the catalogue names, so a typo
# ({registration_numer}) and a hopeful invention ({numéro de TVA}) are both
# caught at edition instead of reaching a client verbatim.
_TOKEN_PATTERN = re.compile(r"\{([^{}\n]{1,64})\}")


@dataclass(frozen=True)
class TokenSpec:
    """`name` doubles as the front's i18n key; `label` is the served human
    fallback (FR), so a token this front does not know still displays a
    sentence instead of an identifier."""

    name: str
    label: str
    read: Callable[[Agency], str | None]


CATALOGUE: tuple[TokenSpec, ...] = (
    # The brand name — the historical token, always filled (NOT NULL).
    TokenSpec("agency_name", "le nom de votre agence", lambda agency: agency.name),
    # The legal identity, one token per column of « Profil & marque ».
    TokenSpec("legal_name", "votre dénomination légale", lambda agency: agency.legal_name),
    TokenSpec("legal_form", "votre forme juridique", lambda agency: agency.legal_form),
    TokenSpec(
        "registration_number",
        "votre numéro d'immatriculation",
        lambda agency: agency.registration_number,
    ),
    TokenSpec("address", "votre adresse", lambda agency: agency.address),
    TokenSpec("postal_code", "votre code postal", lambda agency: agency.postal_code),
    TokenSpec("city", "votre ville", lambda agency: agency.city),
    # Stored ISO 3166-1 alpha-2: renders «FR», exactly as the generated
    # identity sentence already does.
    TokenSpec("country", "votre pays", lambda agency: agency.country),
    TokenSpec("contact_email", "votre email de contact", lambda agency: agency.contact_email),
    TokenSpec("contact_phone", "votre téléphone de contact", lambda agency: agency.contact_phone),
)

_BY_NAME: dict[str, TokenSpec] = {spec.name: spec for spec in CATALOGUE}


def _value(spec: TokenSpec, agency: Agency) -> str | None:
    """The trimmed value if it carries content, else None (« non renseigné »)."""
    raw = spec.read(agency)
    return raw.strip() if raw and raw.strip() else None


def resolve(content: str, agency: Agency) -> str:
    """Every KNOWN token replaced by its current value; everything else left
    exactly as written. THE read-time resolution — the client screen, the
    pending payloads and the edition preview all go through here, so they
    can never render differently."""
    for spec in CATALOGUE:
        placeholder = "{" + spec.name + "}"
        if placeholder in content:
            content = content.replace(placeholder, _value(spec, agency) or "")
    return content


def _used(contents: tuple[str | None, ...]) -> list[str]:
    """The DISTINCT brace-wrapped names of the given texts, in reading order."""
    seen: list[str] = []
    for content in contents:
        if not content:
            continue
        for name in _TOKEN_PATTERN.findall(content):
            if name not in seen:
                seen.append(name)
    return seen


def unknown_tokens(*contents: str | None) -> list[str]:
    """Tokens nobody resolves — rendered verbatim to the reader, so they are
    named at EDITION. Returned brace-wrapped, as the agency typed them."""
    return ["{" + name + "}" for name in _used(contents) if name not in _BY_NAME]


def unfilled_tokens(agency: Agency, *contents: str | None) -> list[str]:
    """Known tokens USED by the texts whose profile field is empty: the
    client reads a blank there. Distinct from `unknown_tokens` (a blank, not
    a leaked token) and from `missing_legal_fields` (which names every empty
    field, used or not) — this one is the subset the text actually depends
    on."""
    return [
        "{" + name + "}"
        for name in _used(contents)
        if name in _BY_NAME and _value(_BY_NAME[name], agency) is None
    ]


def token_values(agency: Agency) -> list[tuple[str, str, str | None]]:
    """The catalogue as (name, label, current value) — what the contract
    serves so the front guesses no list. None = « non renseigné »."""
    return [(spec.name, spec.label, _value(spec, agency)) for spec in CATALOGUE]
