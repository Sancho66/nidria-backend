"""document.kind + document.person_id (GAP-B: the per-case deliverable)

Additive: `kind` (deposit|deliverable, default deposit — the agency
chooses at deposit) and `person_id` (optional member targeting: Claire's
translation visible to Claire; SET NULL on person removal).

Revision ID: b5d1f7c3e9a6
Revises: a4c0e6b2d8f5
Create Date: 2026-07-19 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5d1f7c3e9a6"
down_revision: str | Sequence[str] | None = "a4c0e6b2d8f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column("kind", sa.String(length=20), server_default="deposit", nullable=False),
    )
    op.add_column("document", sa.Column("person_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_document_person_id",
        "document",
        "case_person",
        ["person_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_document_person_id", "document", type_="foreignkey")
    op.drop_column("document", "person_id")
    op.drop_column("document", "kind")
