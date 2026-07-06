"""add ai_translation_job (async AI translation with per-lot progress)

RLS enabled (post-sweep rule). Additive, cleanly reversible.

Revision ID: c8e5f1b3d7a0
Revises: b7d4e0a2c6f8
Create Date: 2026-07-05 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e5f1b3d7a0"
down_revision: Union[str, Sequence[str], None] = "b7d4e0a2c6f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_translation_job",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "langs", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("progress_done", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("progress_total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("translated_keys", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("points_charged", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.String(length=120), nullable=True),
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
        sa.ForeignKeyConstraint(["template_id"], ["journey_template.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_translation_job_agency_id"), "ai_translation_job", ["agency_id"])
    op.create_index(
        op.f("ix_ai_translation_job_template_id"), "ai_translation_job", ["template_id"]
    )
    op.execute("ALTER TABLE ai_translation_job ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_translation_job_template_id"), table_name="ai_translation_job")
    op.drop_index(op.f("ix_ai_translation_job_agency_id"), table_name="ai_translation_job")
    op.drop_table("ai_translation_job")
