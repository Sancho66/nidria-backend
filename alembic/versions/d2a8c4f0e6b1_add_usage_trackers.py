"""add usage trackers (bloc 1): usage_event + agency_usage_milestone,
client_case.is_demo, agency.trial_ends_at

Layer 1 (insert-only agency-scoped events) + layer 2 (adoption
milestones, immutable first_at) + the demo-case exclusion flag + the
trial model. RLS enabled on the two new tables (post-sweep rule).
Additive, cleanly reversible.

Revision ID: d2a8c4f0e6b1
Revises: c1f7b3e9d5a4
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2a8c4f0e6b1"
down_revision: Union[str, Sequence[str], None] = "c1f7b3e9d5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usage_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", sa.String(length=10), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agency_id"], ["agency.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_usage_event_agency_type_created",
        "usage_event",
        ["agency_id", "event_type", "created_at"],
    )
    op.create_table(
        "agency_usage_milestone",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
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
        sa.UniqueConstraint("agency_id", "key", name="uq_agency_usage_milestone"),
    )
    op.create_index(
        op.f("ix_agency_usage_milestone_agency_id"), "agency_usage_milestone", ["agency_id"]
    )
    op.add_column(
        "client_case",
        sa.Column("is_demo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("agency", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    for table in ("usage_event", "agency_usage_milestone"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_column("agency", "trial_ends_at")
    op.drop_column("client_case", "is_demo")
    op.drop_index(
        op.f("ix_agency_usage_milestone_agency_id"), table_name="agency_usage_milestone"
    )
    op.drop_table("agency_usage_milestone")
    op.drop_index("ix_usage_event_agency_type_created", table_name="usage_event")
    op.drop_table("usage_event")
