"""Minimal case PDF: key info + activity journal. fpdf2 — pure Python,
no system dependency (vs WeasyPrint's pango/cairo)."""

from fpdf import FPDF

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser


def _latin1(value: str) -> str:
    # Core PDF fonts are latin-1; replace anything beyond rather than crash.
    return value.encode("latin-1", "replace").decode("latin-1")


def build_case_pdf(
    *,
    case: ClientCase,
    principal: ExpatUser,
    owner: Agent | None,
    activity_rows: list[ActivityLog],
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
        f"Status: {case.status}",
        f"Owner: {owner.first_name + ' ' + owner.last_name if owner else '—'}",
        f"Tags: {', '.join(case.tags) if case.tags else '—'}",
        f"Source: {case.source or '—'}",
        f"Created: {case.created_at:%Y-%m-%d}",
    ]
    for line in info_lines:
        pdf.cell(0, 7, _latin1(line))
        pdf.ln(7)

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
