from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.email import NormalizedEmailStr
from src.core.i18n import Language

# --- Acquisition ------------------------------------------------------------
# La source d'une inscription est PÉRISSABLE : elle n'existe que dans l'URL
# d'arrivée et le referrer, et disparaît au premier clic. Le front la capture
# à l'atterrissage sur /signup ; ces champs sont le contrat qui lui permet de
# l'émettre. Optionnels partout : une inscription directe reste valide.
# Bornés ET assainis côté back aussi — c'est une entrée PUBLIQUE non
# authentifiée, la validation du front n'est pas une garantie.
_ACQUISITION_MAX = 200


def _clean_acquisition(value: str | None) -> str | None:
    """Sauts de ligne aplatis, espaces réduits, bornage dur, vide → None (un
    champ creux ne doit pas bloquer une vraie capture plus tard)."""
    if value is None:
        return None
    flat = " ".join(value.split())
    return flat[:_ACQUISITION_MAX] or None


class AcquisitionFields(BaseModel):
    """Les quatre champs, mixés dans LES DEUX étapes d'inscription : l'étape 1
    est la première touche (celle qui compte), l'étape 3 est le filet si le
    visiteur a rechargé entre-temps."""

    utm_source: str | None = Field(default=None, max_length=_ACQUISITION_MAX)
    utm_medium: str | None = Field(default=None, max_length=_ACQUISITION_MAX)
    utm_campaign: str | None = Field(default=None, max_length=_ACQUISITION_MAX)
    referrer: str | None = Field(default=None, max_length=_ACQUISITION_MAX)

    @field_validator("utm_source", "utm_medium", "utm_campaign", "referrer", mode="before")
    @classmethod
    def _sanitize(cls, v: str | None) -> str | None:
        return _clean_acquisition(v)


class SignupRequest(AcquisitionFields):
    """Stage 1: the email to verify. `website` is the HONEYPOT (hidden
    field): humans leave it empty, bots fill it — non-empty = silent 200,
    nothing created. `turnstile_token` is required only when the
    TURNSTILE_SECRET flag is armed. extra=forbid: an unknown field (e.g.
    sectors, which belong to /signup/complete) is a LOUD 422, never a
    silent swallow."""

    model_config = ConfigDict(extra="forbid")

    email: NormalizedEmailStr
    lang: Language = "fr"
    website: str | None = None
    turnstile_token: str | None = None


class SignupAccepted(BaseModel):
    """Always the same body, email known or not (forgot-password pattern:
    the EMAIL differs, never the response)."""

    status: str = "sent"


class SignupVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: NormalizedEmailStr
    code: str = Field(min_length=6, max_length=6)


class SignupVerifyResponse(BaseModel):
    """The short-lived (30 min) completion token — long and
    unbruteforcable, the only key /signup/complete accepts."""

    completion_token: str


class SignupCompleteRequest(AcquisitionFields):
    model_config = ConfigDict(extra="forbid")

    completion_token: str = Field(min_length=16, max_length=64)
    agency_name: str = Field(min_length=1, max_length=200)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8)
    language: Language = "fr"
    referral_code: str | None = Field(default=None, min_length=4, max_length=16)
    # Sector(s) chosen IN the signup form (no post-signup wall): mandatory,
    # written atomically with the agency. >= 1 else 422 signup.sectors_required;
    # values validated (enum + dedup) in the manager.
    sectors: list[str] | None = None
