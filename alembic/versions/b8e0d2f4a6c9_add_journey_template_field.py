"""add journey_template_field (per-template field collection, vague 1)

The explicit list of fields a template collects at case creation —
separate from step requirements. Additive.

Revision ID: b8e0d2f4a6c9
Revises: a7d9c1e3f5b4
Create Date: 2026-06-15 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b8e0d2f4a6c9"
down_revision: Union[str, Sequence[str], None] = "a7d9c1e3f5b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "journey_template_field",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "required_at_creation",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["template_id"], ["journey_template.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "kind", "reference", name="uq_journey_template_field"),
    )
    op.create_index(
        op.f("ix_journey_template_field_template_id"),
        "journey_template_field",
        ["template_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_journey_template_field_template_id"), table_name="journey_template_field"
    )
    op.drop_table("journey_template_field")
