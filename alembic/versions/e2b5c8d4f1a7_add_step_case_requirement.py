"""add step_case_requirement (case-level step requirements, vague C1)

Declaration only: a template step may require a client_case column
(country/address). NO person_id, NO scope, NO concrete materialized
table — a case field's value is derived live from client_case (option B
+ feuille de C). Purely additive: 1 CREATE TABLE, nothing else touched.

Revision ID: e2b5c8d4f1a7
Revises: d1a4f7c2e9b6
Create Date: 2026-06-16 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e2b5c8d4f1a7"
down_revision: Union[str, Sequence[str], None] = "d1a4f7c2e9b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "step_case_requirement",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_field", sa.String(length=30), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["step_id"], ["journey_template_step.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("step_id", "case_field", name="uq_step_case_requirement"),
    )
    op.create_index(
        op.f("ix_step_case_requirement_step_id"),
        "step_case_requirement",
        ["step_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_step_case_requirement_step_id"), table_name="step_case_requirement"
    )
    op.drop_table("step_case_requirement")
