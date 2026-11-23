"""Generate the two editable client documents from the firm's own profile."""

from shared.models.agency import Agency
from src.consents.client_templates import (
    TEMPLATES,
    ClientText,
    document_language,
    has_operator_identity,
    is_operator_value,
    render_template,
)
from src.core.countries import country_name

# The response schema uses the French fallback before the agency is known.
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


def _address(agency: Agency, lang: str) -> str | None:
    """The seat as an ADDRESS LINE, so its last part is a country NAME in the
    language of the document — « Asunción, Paraguay », not « Asunción, PY ».
    The column keeps the ISO code (what a selector and a filter want); only
    this rendering resolves it, which also covers a legacy lowercase code."""
    present = [
        part
        for part in (
            _f(agency.address),
            _f(agency.postal_code),
            _f(agency.city),
            country_name(agency.country, lang),
        )
        if part
    ]
    return ", ".join(present) if present else None


def _generate(agency: Agency, kind: ClientText) -> str:
    lang = document_language(agency)
    labels = TEMPLATES[lang]
    name = labels["firm"] if is_operator_value(agency.name) else "{agency_name}"
    if has_operator_identity(agency):
        return render_template(kind, lang, name, name)

    segments = [name]
    for label, field in (
        (labels["name"], agency.legal_name),
        (labels["form"], agency.legal_form),
        (labels["registration"], agency.registration_number),
        (labels["address"], _address(agency, lang)),
    ):
        value = _f(field)
        if value:
            segments.append(label.format(value))
    email, phone = _f(agency.contact_email), _f(agency.contact_phone)
    if email and phone:
        segments.append(labels["both"].format(email, phone))
    elif email:
        segments.append(labels["email"].format(email))
    elif phone:
        segments.append(labels["phone"].format(phone))
    return render_template(kind, lang, name, ", ".join(segments))


def generate_client_terms(agency: Agency) -> str:
    return _generate(agency, "terms")


def generate_client_privacy(agency: Agency) -> str:
    return _generate(agency, "privacy")
