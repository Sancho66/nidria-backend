"""add case_external_assignment (per-case external scoping, wave B)

The single fact the external per-case scoping filters on: an external
agent accesses a case iff a row links them. Additive.

Revision ID: f5c7e9a1b3d2
Revises: e4b6d8f0a2c1
Create Date: 2026-06-15 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f5c7e9a1b3d2"
down_revision: Union[str, Sequence[str], None] = "e4b6d8f0a2c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "case_external_assignment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["client_case.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_by_agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "agent_id", name="uq_case_external_assignment"),
    )
    op.create_index(
        op.f("ix_case_external_assignment_case_id"),
        "case_external_assignment",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_external_assignment_agent_id"),
        "case_external_assignment",
        ["agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_case_external_assignment_agent_id"), table_name="case_external_assignment"
    )
    op.drop_index(
        op.f("ix_case_external_assignment_case_id"), table_name="case_external_assignment"
    )
    op.drop_table("case_external_assignment")
