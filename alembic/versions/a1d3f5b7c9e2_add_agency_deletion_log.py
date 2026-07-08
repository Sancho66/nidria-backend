"""add agency_deletion_log (hard agency deletion trace, Groupe C)

Insert-only superadmin audit row per hard-deleted agency; NO FK to
agency (frozen name/slug/id). RLS enabled (post-sweep rule). Additive,
cleanly reversible.

Revision ID: a1d3f5b7c9e2
Revises: f0c2e8a4b6d1
Create Date: 2026-07-08 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1d3f5b7c9e2"
down_revision: Union[str, Sequence[str], None] = "f0c2e8a4b6d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agency_deletion_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deleted_agency_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agency_name", sa.String(length=200), nullable=False),
        sa.Column("agency_slug", sa.String(length=100), nullable=False),
        sa.Column("deleted_cases_count", sa.Integer(), nullable=False),
        sa.Column("performed_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("performed_by_email", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("ALTER TABLE agency_deletion_log ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("agency_deletion_log")
