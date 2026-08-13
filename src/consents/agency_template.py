"""Generate the agency-NAMED client documents (lot 13/08).

Decision (Alexandre): rather than block an agency without its own terms
(which made its clients accept NIDRIA's text — legally untenable, they are
not our clients), we furnish a MODEL in the agency's name, pre-filled from
what we already know and immediately published as the agency's own document
(the Nidria fallback dies). What we do NOT know appears as a VISIBLE marker
(«[Votre numéro d'immatriculation]») — never a silent gap, never an
invention. The markers are what push the agency to complete its profile.

The brand name stays the `{agency_name}` token (resolved at read time to
agency.name, like the canonical texts); the LEGAL identity fields are baked
in at generation (and regenerated when the profile changes, until the
agency validates). The responsibility disclaimer is served at EDITION only,
never inside the text shown to the client.
"""

from shared.models.agency import Agency

# Shown at edition (Settings), NEVER inside the client-facing document.
RESPONSIBILITY_DISCLAIMER = (
    "Modèle fourni à titre indicatif — vous êtes seul responsable des conditions "
    "que vous diffusez à vos clients. Complétez les champs entre crochets et "
    "adaptez le texte à votre activité."
)


def _or_marker(value: str | None, marker: str) -> str:
    """The value if present, else a VISIBLE bracketed marker."""
    return value.strip() if value and value.strip() else f"[{marker}]"


def _identity_block(agency: Agency) -> str:
    """The legal identity header — each missing field a visible marker."""
    denomination = _or_marker(agency.legal_name, "Votre dénomination légale")
    form = _or_marker(agency.legal_form, "Votre forme juridique")
    registration = _or_marker(agency.registration_number, "Votre numéro d'immatriculation")
    parts = [agency.address, agency.postal_code, agency.city, agency.country]
    address = ", ".join(p.strip() for p in parts if p and p.strip()) or "[Votre adresse]"
    email = _or_marker(agency.contact_email, "Votre email de contact")
    return (
        f"**{denomination}** ({form}), immatriculée sous le numéro {registration}, "
        f"dont le siège est situé {address}, joignable à l'adresse {email}."
    )


def generate_client_terms(agency: Agency) -> str:
    """The agency's own « Conditions d'utilisation de l'espace client »."""
    identity = _identity_block(agency)
    return f"""# Conditions d'utilisation de l'espace client

Ces conditions régissent l'utilisation de l'espace client mis à votre disposition par
{{agency_name}}.

Éditeur de l'espace : {identity}

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

1. Responsable de traitement : {{agency_name}} — {identity} — est responsable du traitement de
   vos données personnelles dans le cadre de votre dossier.
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
