"""add journey_template_case_field (per-template case-field collection, option b)

Case-level fields (origin/destination country) a journey collects at
case creation. SEPARATE from journey_template_field (person fields).
Additive — NO ALTER on client_case: the country columns and their whole
query ecosystem (filters/sorts/dashboard/views/faces/export) are
untouched.

Revision ID: c9f1a3e5d7b2
Revises: b8e0d2f4a6c9
Create Date: 2026-06-16 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c9f1a3e5d7b2"
down_revision: Union[str, Sequence[str], None] = "b8e0d2f4a6c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "journey_template_case_field",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_field", sa.String(length=30), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "required_at_creation",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["template_id"], ["journey_template.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_id", "case_field", name="uq_journey_template_case_field"
        ),
    )
    op.create_index(
        op.f("ix_journey_template_case_field_template_id"),
        "journey_template_case_field",
        ["template_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_journey_template_case_field_template_id"),
        table_name="journey_template_case_field",
    )
    op.drop_table("journey_template_case_field")
