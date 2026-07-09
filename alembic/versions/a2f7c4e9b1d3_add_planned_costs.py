"""add_planned_costs

Revision ID: a2f7c4e9b1d3
Revises: f5c2a8d1e9b3
Create Date: 2026-07-10

Planned costs on journey template steps (Reside). One migration:
- create `journey_step_cost` (the template planned line: amount DECIMAL(18,4) +
  label, CASCADE with its step);
- evolve `case_step_cost` so a line carries PLANNED and REAL side by side:
  `amount` (the real sum) becomes NULLABLE — empty until the agency pays;
  add `planned_amount` (frozen by value at instantiation); add
  `source_template_cost_id` (a dead trace to the template line, SET NULL).

Reversible + idempotent (proven on a testcontainer). Downgrade restores the
NOT NULL on `amount` after zeroing any still-empty line (lossy, acknowledged:
the feature is being reverted).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a2f7c4e9b1d3"
down_revision: str | Sequence[str] | None = "f5c2a8d1e9b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_SOURCE = "fk_case_step_cost_source_template_cost_id"


def upgrade() -> None:
    # The template planned line — created BEFORE the FK on case_step_cost.
    op.create_table(
        "journey_step_cost",
        sa.Column("step_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
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
            ["step_id"], ["journey_template_step.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_journey_step_cost_step_id"),
        "journey_step_cost",
        ["step_id"],
        unique=False,
    )
    # A real cost is EMPTY until paid (a planned line starts with amount NULL).
    op.alter_column(
        "case_step_cost",
        "amount",
        existing_type=sa.Numeric(precision=18, scale=4),
        nullable=True,
    )
    # Planned amount, frozen by value at instantiation (no propagation).
    op.add_column(
        "case_step_cost",
        sa.Column("planned_amount", sa.Numeric(precision=18, scale=4), nullable=True),
    )
    # A dead trace to the template planned cost (SET NULL when it is deleted;
    # planned_amount survives untouched).
    op.add_column(
        "case_step_cost",
        sa.Column("source_template_cost_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        _FK_SOURCE,
        "case_step_cost",
        "journey_step_cost",
        ["source_template_cost_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_FK_SOURCE, "case_step_cost", type_="foreignkey")
    op.drop_column("case_step_cost", "source_template_cost_id")
    op.drop_column("case_step_cost", "planned_amount")
    # Restore NOT NULL: a planned-but-unpaid line has amount NULL — zero it so
    # the rollback doesn't fail on real data (lossy, the feature is reverted).
    op.execute("UPDATE case_step_cost SET amount = 0 WHERE amount IS NULL")
    op.alter_column(
        "case_step_cost",
        "amount",
        existing_type=sa.Numeric(precision=18, scale=4),
        nullable=False,
    )
    op.drop_index(op.f("ix_journey_step_cost_step_id"), table_name="journey_step_cost")
    op.drop_table("journey_step_cost")
