"""add step content — content_note + journey_step_attachment (Feature 2, V1)

Descending content the agency provides on a TEMPLATE step (a note +
attachments), distinct from step requirements. Additive: 1 nullable
column on journey_template_step + 1 new table. Does NOT touch client_case
nor the country ecosystem.

Revision ID: a1d4f8c2e6b9
Revises: a3e7c1f9b5d2
Create Date: 2026-06-18 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1d4f8c2e6b9"
down_revision: Union[str, Sequence[str], None] = "a3e7c1f9b5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("journey_template_step", sa.Column("content_note", sa.Text(), nullable=True))
    op.create_table(
        "journey_step_attachment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("uploaded_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["step_id"], ["journey_template_step.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["uploaded_by_agent_id"], ["agent.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_journey_step_attachment_step_id"),
        "journey_step_attachment",
        ["step_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_journey_step_attachment_step_id"), table_name="journey_step_attachment"
    )
    op.drop_table("journey_step_attachment")
    op.drop_column("journey_template_step", "content_note")
