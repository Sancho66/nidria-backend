"""Reproducible fictional PDFs, served by the real document download handler."""

from fpdf import FPDF

from src.core.storage import mock_store
from src.demo_sandbox.catalog import DemoCatalog


def document_path(record_id: str, position: int) -> str:
    return f"demo-sandbox/{record_id}/{position}.pdf"


def load_demo_documents(catalog: DemoCatalog) -> None:
    for record in catalog.records:
        labels = [doc["label"] for doc in record.state["documents"] if doc["status"] == "validated"]
        labels.append(record.state["signature"]["label"])
        for position, label in enumerate(labels):
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 16)
            title = (
                "DEMONSTRATION - FICTIONAL DATA"
                if record.language == "en"
                else "DEMONSTRATION - DONNEES FICTIVES"
            )
            pdf.multi_cell(0, 10, title)
            pdf.set_font("Helvetica", size=12)
            for line in (record.name, label, record.state["signature"]["notice"]):
                # The PDF's core font is latin-1. No source text or UI is altered.
                pdf.set_x(pdf.l_margin)
                pdf.multi_cell(0, 9, line.encode("latin-1", "replace").decode("latin-1"))
            mock_store[document_path(record.id, position)] = bytes(pdf.output())
