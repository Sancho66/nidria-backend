"""Generate the agency-NAMED client documents (lot 13/08, omission 13/08).

Decision (Alexandre): rather than block an agency without its own terms
(which made its clients accept NIDRIA's text — legally untenable, they are
not our clients), we furnish a MODEL in the agency's name, pre-filled from
what we already know and immediately published as the agency's own document
(the Nidria fallback dies).

Omission by SEGMENT (13/08): the identity sentence is composed from the
FILLED fields only — each optional segment («immatriculée sous le numéro X»,
«dont le siège est situé Y», …) drops when its data is missing, so the
published text stays GRAMMATICAL at every fill level (no orphan comma, no
«sous le numéro .», no empty slot). No BRACKETS in the published document:
signalling the missing fields is the job of the Settings warning (which
reads `missing_legal_fields`), not of the text the client accepts.

The brand name stays the `{agency_name}` token (resolved at read time to
agency.name, like the canonical texts); the legal facts are baked in at
generation (and regenerated when the profile changes, until the agency
validates). The responsibility disclaimer is served at EDITION only, never
inside the text shown to the client.
"""

from shared.models.agency import Agency

# Shown at edition (Settings), NEVER inside the client-facing document.
RESPONSIBILITY_DISCLAIMER = (
    "Modèle fourni à titre indicatif — vous êtes seul responsable des conditions "
    "que vous diffusez à vos clients. Complétez votre profil et adaptez le texte à "
    "votre activité."
)

# The Profil & marque fields, in display order — the ones the front lets the
# agency fill and whose absence its warning names.
_LEGAL_FIELDS: tuple[str, ...] = (
    "legal_name",
    "legal_form",
    "registration_number",
    "address",
    "city",
    "postal_code",
    "country",
    "contact_email",
    "contact_phone",
)


def _f(value: str | None) -> str | None:
    """The trimmed value if it carries content, else None."""
    return value.strip() if value and value.strip() else None


def missing_legal_fields(agency: Agency) -> list[str]:
    """The Profil & marque fields still empty — served to the front so its
    warning can name them (the brackets left the published text)."""
    return [name for name in _LEGAL_FIELDS if _f(getattr(agency, name)) is None]


def _address(agency: Agency) -> str | None:
    present = [
        part
        for part in (
            _f(agency.address),
            _f(agency.postal_code),
            _f(agency.city),
            _f(agency.country),
        )
        if part
    ]
    return ", ".join(present) if present else None


def _identity_block(agency: Agency) -> str:
    """The legal identity as a grammatical clause, anchored on the
    {agency_name} token and extended by ONE segment per filled field. Empty
    profile → just «{agency_name}». Every fill level reads correctly."""
    segments: list[str] = []
    legal_name = _f(agency.legal_name)
    if legal_name:
        segments.append(f"dénommée {legal_name}")
    legal_form = _f(agency.legal_form)
    if legal_form:
        segments.append(f"de forme {legal_form}")
    registration = _f(agency.registration_number)
    if registration:
        segments.append(f"immatriculée sous le numéro {registration}")
    address = _address(agency)
    if address:
        segments.append(f"dont le siège est situé {address}")
    # UN SEUL segment « joignable », qui porte l'email, le téléphone ou les
    # deux. Deux segments séparés donneraient « joignable à x@y, joignable au
    # 01… » — grammatical mais bègue. L'omission par segment est intacte : le
    # segment disparaît entièrement quand les deux champs sont vides.
    email = _f(agency.contact_email)
    phone = _f(agency.contact_phone)
    if email and phone:
        segments.append(f"joignable à {email} et au {phone}")
    elif email:
        segments.append(f"joignable à {email}")
    elif phone:
        segments.append(f"joignable au {phone}")
    if not segments:
        return "{agency_name}"
    return "{agency_name}, " + ", ".join(segments)


def generate_client_terms(agency: Agency) -> str:
    """The agency's own « Conditions d'utilisation de l'espace client »."""
    identity = _identity_block(agency)
    return f"""# Conditions d'utilisation de l'espace client

Ces conditions régissent l'utilisation de l'espace client mis à votre disposition par
{{agency_name}}. Cet espace est édité par {identity}.

1. Cet espace vous est fourni par {{agency_name}}, avec l'outil Nidria, pour suivre l'avancement
   de votre dossier, échanger avec {{agency_name}} et transmettre les informations et documents
   demandés. Il est gratuit pour vous.
2. Vos identifiants sont personnels ; vous êtes responsable de leur confidentialité et de
   l'usage de votre compte.
3. Les informations et documents que vous déposez doivent être exacts, à jour et concerner
   votre dossier. Vous vous interdisez tout contenu illicite.
4. {{agency_name}} reste votre interlocuteur pour toute question relative à votre dossier, aux
   délais et aux décisions qui le concernent. Nidria fournit l'outil et n'intervient pas dans
   votre dossier.
5. Votre accès peut être fermé par {{agency_name}} à la clôture de votre dossier ou à la fin de
   sa relation avec vous. Vous pouvez demander à {{agency_name}} une copie des documents que vous
   avez déposés.
"""


def generate_client_privacy(agency: Agency) -> str:
    """The agency's own « Note d'information sur vos données » (RGPD)."""
    identity = _identity_block(agency)
    return f"""# Note d'information sur vos données

1. Responsable de traitement : le responsable du traitement de vos données personnelles, dans
   le cadre de votre dossier, est {identity}.
2. Sous-traitant : Nidria (BETTERSOFT LLC) héberge et traite ces données pour le compte de
   {{agency_name}}, en qualité de sous-traitant au sens du RGPD, dans le cadre d'un accord de
   traitement des données.
3. Données traitées : votre identité et vos coordonnées, les documents que vous déposez et vos
   échanges avec {{agency_name}} dans cet espace.
4. Hébergement : vos données sont hébergées dans l'Union européenne (Paris, France). Elles ne
   sont ni vendues, ni utilisées pour entraîner des modèles d'intelligence artificielle.
5. Durée : vos données sont conservées pendant la durée de votre dossier et de la relation avec
   {{agency_name}}, puis selon les obligations légales qui s'imposent à {{agency_name}}.
6. Vos droits : vous disposez des droits d'accès, de rectification, d'effacement, de limitation,
   d'opposition et de portabilité. Pour les exercer, adressez-vous à {{agency_name}}, votre
   interlocuteur unique ; Nidria l'assiste techniquement. Vous pouvez également saisir l'autorité
   de contrôle compétente (en France, la CNIL).
"""
