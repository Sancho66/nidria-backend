"""Minimal case PDF: key info + activity journal. fpdf2 — pure Python,
no system dependency (vs WeasyPrint's pango/cairo)."""

from fpdf import FPDF

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.expat_user import ExpatUser
from src.core.enums import CasePersonKind
from src.core.i18n import DEFAULT_LANG, resolve_i18n


def _latin1(value: str) -> str:
    # Core PDF fonts are latin-1; replace anything beyond rather than crash.
    return value.encode("latin-1", "replace").decode("latin-1")


def _address(street: str | None, postal: str | None, city: str | None, country: str | None) -> str:
    parts = [p for p in (street, " ".join(x for x in (postal, city) if x), country) if p]
    return ", ".join(parts) if parts else "—"


def _custom_lines(
    person: CasePerson,
    definitions: list[CustomFieldDefinition],
    lang: str,
    agency_default: str,
) -> list[str]:
    """Agency custom fields (label: value) — only active definitions with
    a saved value. Multi-select values are joined. The LABEL is resolved for
    `lang` (BLOC 2); the stored value is keyed by the untranslated `key`."""
    stored = person.custom_fields or {}
    out: list[str] = []
    for definition in definitions:
        if definition.key not in stored:
            continue
        value = stored[definition.key]
        rendered = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        label = resolve_i18n(definition.label_i18n, lang, agency_default, definition.label)
        out.append(f"{label}: {rendered}")
    return out


def _civil_lines(
    label: str,
    person: CasePerson,
    definitions: list[CustomFieldDefinition],
    lang: str,
    agency_default: str,
) -> list[str]:
    """Civil-status + custom-field lines for one person — only filled."""
    fields = [
        ("Passport", person.passport_number),
        ("Date of birth", person.date_of_birth.isoformat() if person.date_of_birth else None),
        ("Nationality", person.nationality),
        ("Place of birth", person.place_of_birth),
        ("Sex", person.sex),
        ("Marital status", person.marital_status),
        ("Phone", person.phone),
    ]
    filled = [f"{k}: {v}" for k, v in fields if v]
    filled += _custom_lines(person, definitions, lang, agency_default)
    if not filled:
        return [f"{label}: (no details)"]
    return [f"{label}:", *[f"  - {line}" for line in filled]]


def build_case_pdf(
    *,
    case: ClientCase,
    principal: ExpatUser,
    owner: Agent | None,
    persons: list[CasePerson],
    custom_field_definitions: list[CustomFieldDefinition],
    activity_rows: list[ActivityLog],
    lang: str = DEFAULT_LANG,
    agency_default: str = DEFAULT_LANG,
) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _latin1(f"Case file — {principal.first_name} {principal.last_name}"))
    pdf.ln(12)

    pdf.set_font("Helvetica", "", 11)
    info_lines = [
        f"Client: {principal.first_name} {principal.last_name} ({principal.email})",
        f"Route: {case.origin_country or '—'} -> {case.dest_country or '—'}",
        "Origin address: "
        + _address(
            case.origin_street, case.origin_postal_code, case.origin_city, case.origin_country
        ),
        "Destination address: "
        + _address(case.dest_street, case.dest_postal_code, case.dest_city, case.dest_country),
        f"Status: {case.status}",
        f"Owner: {owner.first_name + ' ' + owner.last_name if owner else '—'}",
        f"Tags: {', '.join(case.tags) if case.tags else '—'}",
        f"Source: {case.source or '—'}",
        f"Created: {case.created_at:%Y-%m-%d}",
    ]
    for line in info_lines:
        pdf.cell(0, 7, _latin1(line))
        pdf.ln(7)

    # People + civil status.
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 9, "People")
    pdf.ln(10)
    pdf.set_font("Helvetica", "", 10)
    for person in persons:
        if person.kind == CasePersonKind.PRINCIPAL.value:
            label = f"Principal — {principal.first_name} {principal.last_name}"
        else:
            rel = f" ({person.relationship})" if person.relationship else ""
            label = f"{person.full_name or '—'}{rel}"
        for line in _civil_lines(label, person, custom_field_definitions, lang, agency_default):
            pdf.cell(0, 6, _latin1(line))
            pdf.ln(6)

    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 9, "Activity journal")
    pdf.ln(10)
    pdf.set_font("Helvetica", "", 10)
    if not activity_rows:
        pdf.cell(0, 6, "No activity yet.")
        pdf.ln(6)
    for row in activity_rows:
        line = f"{row.created_at:%Y-%m-%d %H:%M} - [{row.actor_type}] {row.action_type}"
        pdf.cell(0, 6, _latin1(line))
        pdf.ln(6)

    return bytes(pdf.output())
