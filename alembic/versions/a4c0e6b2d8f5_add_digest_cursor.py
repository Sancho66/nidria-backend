"""digest_cursor (progress digest, the last notifications piece)

One row per agency: the point up to which activity has been digested.
A table rather than a settings key: the front's settings PATCH replaces
the whole JSONB and would wipe a key-based cursor. RLS enabled.

Revision ID: a4c0e6b2d8f5
Revises: f3b9d5a1c7e4
Create Date: 2026-07-19 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c0e6b2d8f5"
down_revision: str | Sequence[str] | None = "f3b9d5a1c7e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "digest_cursor",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agency_id", sa.Uuid(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agency_id"], ["agency.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agency_id", name="uq_digest_cursor_agency"),
    )
    op.create_index(
        op.f("ix_digest_cursor_agency_id"), "digest_cursor", ["agency_id"], unique=False
    )
    op.execute("ALTER TABLE digest_cursor ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("digest_cursor")
