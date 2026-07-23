"""i18n resolution for ENTERED content (BLOC 2).

A single resolver, reused everywhere a translatable label is read. Entered
content lives as a `{lang: text}` JSONB blob next to a scalar column (BLOC 1);
this module turns (blob, display language, agency default) into the string to
show, with a deterministic fallback chain that ends on the legacy scalar — so a
missing translation never yields "" or None for a required field.

This is ORTHOGONAL to the frontend's static-UI i18n (createScopedI18n): that
handles chrome at build time; this resolves runtime DB content.
"""

from typing import Annotated, Literal, get_args

from fastapi import Depends, Request

# THE single source of truth for supported content/UI languages. Add a language
# here (and to the `Language` literal just below) and every reader follows: the
# blob filter, the resolvers, the notification routing, the request-language
# negotiation, and the agency.default_language Pydantic schema. The SQL CHECK on
# agency.default_language is widened by a dedicated migration, never here.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("fr", "en", "es", "ru", "pt", "it", "hu")
DEFAULT_LANG = "fr"  # the platform fallback (also the implicit default of samples)

# The Pydantic/OpenAPI face of SUPPORTED_LANGUAGES. mypy needs a STATIC Literal,
# so it is spelled out and kept in lock-step with the tuple by the assert below —
# edit BOTH in the same change when adding a language (the assert fails at import
# otherwise, turning any drift into an immediate startup error).
Language = Literal["fr", "en", "es", "ru", "pt", "it", "hu"]
assert set(get_args(Language)) == set(SUPPORTED_LANGUAGES), (
    "Language literal and SUPPORTED_LANGUAGES drifted apart"
)


def resolve_i18n(
    blob: dict[str, str] | None,
    lang: str,
    agency_default: str,
    scalar: str | None = None,
) -> str | None:
    """Resolve a translatable label to the string to display.

    Fallback chain (first non-empty wins):
      blob[lang] → blob[agency_default] → blob["fr"] → scalar (legacy column).

    Never returns "" (an absent language is an absent key, so empty values fall
    through). Returns None ONLY when the scalar itself is None — i.e. a genuinely
    optional field (content_note, description) with no value anywhere. Required
    fields always have a scalar, hence always resolve to a string.
    """
    b = blob or {}
    return b.get(lang) or b.get(agency_default) or b.get(DEFAULT_LANG) or scalar


def normalize_i18n_input(blob: dict[str, str] | None) -> dict[str, str]:
    """Sanitize an i18n blob coming from a write request: keep only supported
    languages with a NON-EMPTY value. An empty/whitespace value is dropped (an
    absent language is an absent key, never "")."""
    if not blob:
        return {}
    return {k: v for k, v in blob.items() if k in SUPPORTED_LANGUAGES and v and v.strip()}


def apply_i18n_write(
    blob_in: dict[str, str] | None,
    scalar_in: str | None,
    agency_default: str,
    current_scalar: str | None,
    current_blob: dict[str, str] | None,
) -> tuple[str | None, dict[str, str]]:
    """Resolve a write of a translatable field into (new_scalar, new_blob),
    keeping the scalar (the FR anchor / fallback / seed key) in sync with the
    blob so the two never desynchronize.

    - If an i18n blob is provided → it becomes the blob (sanitized); the scalar
      is recomputed as blob[agency_default] → blob[fr] → the incoming scalar →
      the current scalar (so the scalar always equals the default-language
      variant when present).
    - If only the scalar is provided → set the scalar and mirror it into the
      blob under the agency default language (so a future read resolves it).
    - If neither is provided → unchanged.
    """
    if blob_in is not None:
        blob = normalize_i18n_input(blob_in)
        scalar = blob.get(agency_default) or blob.get(DEFAULT_LANG) or scalar_in or current_scalar
        return scalar, blob
    if scalar_in is not None:
        blob = dict(current_blob or {})
        if scalar_in:
            blob[agency_default] = scalar_in
        return scalar_in, blob
    return current_scalar, dict(current_blob or {})


# --- notification language (BLOC NOTIF-1) ------------------------------------
#
# DISTINCT from the display resolution above. A notification is read in the
# RECIPIENT's language, with a recipient-specific fallback that must NOT reuse
# the display chain (which would give an agent's FR default to a client).


def resolve_notification_lang_client(preferred_lang: str | None) -> str:
    """The language of a notification sent to a CLIENT (expat). Their stored
    preferred_lang if supported, else ENGLISH — never the agency default."""
    if preferred_lang and preferred_lang.lower()[:2] in SUPPORTED_LANGUAGES:
        return preferred_lang.lower()[:2]
    return "en"


def resolve_notification_lang_agent(agency_default: str | None) -> str:
    """The language of a notification sent to an AGENT. The agency default if
    supported, else FRENCH (the platform default)."""
    if agency_default and agency_default.lower()[:2] in SUPPORTED_LANGUAGES:
        return agency_default.lower()[:2]
    return DEFAULT_LANG


def resolve_step_name_for_notif(name_i18n: dict[str, str] | None, scalar: str, lang: str) -> str:
    """Resolve a step name for a notification in the recipient's `lang`. The
    recipient language is already resolved (client/agent rules), so the chain
    is blob[lang] → blob[fr] → the scalar. Never empty — the scalar (a required
    field) is the ultimate fallback."""
    resolved = resolve_i18n(name_i18n, lang, lang, scalar)
    return resolved if resolved is not None else scalar


def case_label_for_notif(
    client_first_name: str | None,
    client_last_name: str | None,
    journey_name: str | None,
    journey_name_i18n: dict[str, str] | None,
    lang: str,
) -> str:
    """Human, agent-facing label for a case in a notification — the CLIENT
    name then the JOURNEY name, NEVER the technical UUID (which means nothing
    to an adviser). E.g. "Camille Martin - Création d'un Autónomo en Espagne".
    `lang` is the already-resolved recipient language (journey name resolved
    in it). Degrades gracefully: no journey → the client alone; no client
    name → the journey alone."""
    client = " ".join(p.strip() for p in (client_first_name, client_last_name) if p and p.strip())
    journey = resolve_i18n(journey_name_i18n, lang, lang, journey_name) if journey_name else None
    journey = (journey or "").strip()
    if client and journey:
        return f"{client} - {journey}"
    return client or journey


def resolve_request_language(request: Request) -> str:
    """The USER's display language, mirroring the UI. Channel (first match):
      1. explicit `?lang=` query param,
      2. the `Accept-Language` header (first supported tag),
      3. DEFAULT_LANG.
    Always one of SUPPORTED_LANGUAGES — an unknown/unsupported value falls back to
    the default rather than reaching the resolver as a never-present key."""
    q = request.query_params.get("lang")
    if q and q.lower() in SUPPORTED_LANGUAGES:
        return q.lower()
    header = request.headers.get("accept-language", "")
    for part in header.split(","):
        tag = part.split(";")[0].strip().lower()[:2]
        if tag in SUPPORTED_LANGUAGES:
            return tag
    return DEFAULT_LANG


# The user's display language, injectable in any router endpoint that returns
# translatable content. Passed down to the manager projection methods.
RequestLang = Annotated[str, Depends(resolve_request_language)]
