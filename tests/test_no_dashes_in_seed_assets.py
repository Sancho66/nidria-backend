"""THE dash guard (règle Eric, purge 2026-07-05): U+2014 (em dash) and
U+2013 (en dash) are BANNED from every seeded/outbound text asset — any
future reintroduction breaks the build.

Scope = DATA strings (AST constants), not code comments/docstrings:
internal prose keeps its style, but nothing a user or an agency ever
reads may carry the tell. Legal texts (consents) and nurture mails are
covered too (both were already clean)."""

import ast
import pathlib

# Every file whose string constants end up in the DB (seeds) or in a
# user's inbox/screen (emails, catalogs). Extend when a new textual
# asset appears — never shrink.
GUARDED_FILES = (
    "src/journeys/sample_seed.py",
    "src/journeys/field_catalog.py",
    "scripts/seed.py",
    "src/agencies/demo_case_seed.py",
    "src/nurture/nurture_texts.py",
    "src/consents/consents_texts.py",
    "src/core/email_templates.py",
    "src/views/views_schema.py",
)

BANNED = ("—", "–")  # em dash, en dash

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _data_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """(lineno, value) of every string constant EXCEPT docstrings."""
    tree = ast.parse(path.read_text())
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_dashes_in_seeded_or_outbound_text() -> None:
    offenders: list[str] = []
    for rel in GUARDED_FILES:
        for lineno, value in _data_strings(ROOT / rel):
            if any(ch in value for ch in BANNED):
                offenders.append(f"{rel}:{lineno}: {value[:80]!r}")
    assert offenders == [], "em/en dash reintroduced in text assets:\n" + "\n".join(offenders)
