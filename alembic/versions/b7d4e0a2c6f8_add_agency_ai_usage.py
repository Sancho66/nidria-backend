"""add agency_ai_usage (AI translation quota, monthly points)

One row per (agency, month). RLS enabled (post-sweep rule). Additive,
cleanly reversible.

Revision ID: b7d4e0a2c6f8
Revises: a6f2c8e4d0b9
Create Date: 2026-07-05 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d4e0a2c6f8"
down_revision: Union[str, Sequence[str], None] = "a6f2c8e4d0b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agency_ai_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("points_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.UniqueConstraint("agency_id", "month", name="uq_agency_ai_usage_month"),
    )
    op.create_index(op.f("ix_agency_ai_usage_agency_id"), "agency_ai_usage", ["agency_id"])
    op.execute("ALTER TABLE agency_ai_usage ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index(op.f("ix_agency_ai_usage_agency_id"), table_name="agency_ai_usage")
    op.drop_table("agency_ai_usage")
