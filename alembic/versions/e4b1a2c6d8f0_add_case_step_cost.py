"""add_case_step_cost

Revision ID: e4b1a2c6d8f0
Revises: d3a9c1e5b7f2
Create Date: 2026-07-09

The agency-internal cost line table (Reside). Amount DECIMAL(18,4) — never
float. FK to the STEP INSTANCE (case_step_progress, CASCADE); author SET NULL.
Reversible: drop the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4b1a2c6d8f0"
down_revision: str | Sequence[str] | None = "d3a9c1e5b7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "case_step_cost",
        sa.Column("case_step_progress_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("incurred_on", sa.Date(), nullable=True),
        sa.Column("author_agent_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["case_step_progress_id"], ["case_step_progress.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["author_agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_case_step_cost_case_step_progress_id"),
        "case_step_cost",
        ["case_step_progress_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_case_step_cost_case_step_progress_id"), table_name="case_step_cost")
    op.drop_table("case_step_cost")
