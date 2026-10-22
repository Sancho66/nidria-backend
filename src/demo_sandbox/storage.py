"""Reproducible fictional PDFs, served by the real document download handler."""

import base64
from collections import OrderedDict
from pathlib import Path

from fpdf import FPDF

from src.core.storage import mock_store
from src.demo_sandbox.catalog import DemoCatalog, DemoRecord

FONTS = Path(__file__).resolve().parents[1] / "cases" / "fonts"
_dynamic_files: OrderedDict[str, list[str]] = OrderedDict()


def document_path(record_id: str, position: int) -> str:
    return f"demo-sandbox/{record_id}/{position}.pdf"


def load_demo_documents(catalog: DemoCatalog) -> None:
    for record in catalog.records:
        load_record_documents(record)


def load_record_documents(record: DemoRecord) -> None:
    # New demos are unbounded in the database; generated assets must not be
    # unbounded in this small serving process. Eviction is reconstructible.
    if record.id.startswith("custom-"):
        if record.id in _dynamic_files:
            _dynamic_files.move_to_end(record.id)
        else:
            while len(_dynamic_files) >= 8:
                _, expired = _dynamic_files.popitem(last=False)
                for path in expired:
                    mock_store.pop(path, None)
            _dynamic_files[record.id] = []
    labels = [doc["label"] for doc in record.state["documents"] if doc["status"] == "validated"]
    if record.state["signature"].get("enabled", True):
        labels.append(record.state["signature"]["label"])
    for position, label in enumerate(labels):
        path = document_path(record.id, position)
        if path in mock_store:
            continue
        pdf = FPDF()
        pdf.add_font("Demo", fname=str(FONTS / "DejaVuSans.ttf"))
        pdf.add_font("Demo", style="B", fname=str(FONTS / "DejaVuSans-Bold.ttf"))
        pdf.add_page()
        pdf.set_font("Demo", "B", 16)
        pdf.multi_cell(0, 10, record.state["notice"])
        pdf.set_font("Demo", size=12)
        for line in (record.name, label, record.state["signature"]["notice"]):
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 9, line)
        mock_store[path] = bytes(pdf.output())
        if record.id in _dynamic_files and path not in _dynamic_files[record.id]:
            _dynamic_files[record.id].append(path)
    logo = record.state.get("logo")
    if logo:
        mock_store[logo["path"]] = base64.b64decode(logo["base64"], validate=True)
        if record.id in _dynamic_files and logo["path"] not in _dynamic_files[record.id]:
            _dynamic_files[record.id].append(logo["path"])
