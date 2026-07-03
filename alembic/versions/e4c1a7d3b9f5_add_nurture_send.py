"""add nurture_send (nurture bloc 3): the trial-mail send ledger

One row per (agency, calendar slot) — strict dedup via the unique
constraint. RLS enabled (post-sweep rule). Additive, cleanly reversible.

Revision ID: e4c1a7d3b9f5
Revises: d2a8c4f0e6b1
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4c1a7d3b9f5"
down_revision: Union[str, Sequence[str], None] = "d2a8c4f0e6b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "nurture_send",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day_key", sa.String(length=10), nullable=False),
        sa.Column("mail_key", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recipient", sa.String(length=255), nullable=True),
        sa.Column("lang", sa.String(length=5), server_default=sa.text("'fr'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agency_id"], ["agency.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agency_id", "day_key", name="uq_nurture_send_slot"),
    )
    op.create_index(op.f("ix_nurture_send_agency_id"), "nurture_send", ["agency_id"])
    op.execute("ALTER TABLE nurture_send ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index(op.f("ix_nurture_send_agency_id"), table_name="nurture_send")
    op.drop_table("nurture_send")
