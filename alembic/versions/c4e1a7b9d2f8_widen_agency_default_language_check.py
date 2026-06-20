"""Widen agency.default_language CHECK to 6 languages (fr/en/es + ru/pt/it)

BLOC 1 — capacity only: the platform now supports six content languages. The
column already accepts any 2-char code; only the CHECK constraint pinned the
allowed set to fr/en/es. This drops it and recreates it with the widened set.

Additive (no agency loses a valid value: the old set is a subset of the new),
reversible (downgrade restores the 3-value CHECK — a clean roundtrip as long as
no agency has been switched to ru/pt/it in the meantime, the same caveat as any
enum-narrowing downgrade). No data migration: existing rows stay 'fr'/'en'/'es'.

Revision ID: c4e1a7b9d2f8
Revises: b2d9f4a1c8e3
Create Date: 2026-06-20 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e1a7b9d2f8"
down_revision: str | Sequence[str] | None = "b2d9f4a1c8e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "agency_default_language_check"
_OLD = "default_language IN ('fr', 'en', 'es')"
_NEW = "default_language IN ('fr', 'en', 'es', 'ru', 'pt', 'it')"


def upgrade() -> None:
    op.drop_constraint(_NAME, "agency", type_="check")
    op.create_check_constraint(_NAME, "agency", _NEW)


def downgrade() -> None:
    op.drop_constraint(_NAME, "agency", type_="check")
    op.create_check_constraint(_NAME, "agency", _OLD)
