"""Localized default documents for the firm's clients."""

import json
import re
from pathlib import Path
from typing import Literal, TypedDict

from shared.models.agency import Agency
from src.core.i18n import DEFAULT_LANG, SUPPORTED_LANGUAGES

ClientText = Literal["terms", "privacy"]


class ClientTemplate(TypedDict):
    firm: str
    name: str
    form: str
    registration: str
    address: str
    email: str
    phone: str
    both: str
    disclaimer: str
    terms: list[str]
    privacy: list[str]


TEMPLATES: dict[str, ClientTemplate] = json.loads(
    Path(__file__).with_name("client_templates.json").read_text(encoding="utf-8")
)

# Specific identifiers from the operator's profile or previously copied examples.
# A common legal form, city or country on its own belongs to many independent firms.
_OPERATOR = re.compile(
    r"\b(?:better[\s-]*soft|nidria|expatflow)\b|\b(?:232344242|2024[- ]001529388)\b"
    r"|\b1317\s+edgewater\s+(?:drive|dr\b)",
    re.IGNORECASE,
)


def document_language(agency: Agency) -> str:
    return (
        agency.default_language if agency.default_language in SUPPORTED_LANGUAGES else DEFAULT_LANG
    )


def is_operator_value(value: str | None) -> bool:
    return bool(value and _OPERATOR.search(value))


def has_operator_identity(agency: Agency) -> bool:
    return any(
        is_operator_value(value)
        for value in (
            agency.name,
            agency.legal_name,
            agency.legal_form,
            agency.registration_number,
            agency.address,
            agency.city,
            agency.contact_email,
        )
    )


def render_template(kind: ClientText, lang: str, name: str, identity: str) -> str:
    body = "\n\n".join(TEMPLATES[lang][kind])
    return body.replace("{agency_name}", name).replace("{identity}", identity) + "\n"
