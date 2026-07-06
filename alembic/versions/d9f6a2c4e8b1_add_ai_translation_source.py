"""add ai_translation_source (staleness hash memory of AI translations)

One row per (template, content_key, lang): hash of the translated
SOURCE text + hash of the AI OUTPUT written. RLS enabled (post-sweep
rule). Additive, cleanly reversible.

Revision ID: d9f6a2c4e8b1
Revises: c8e5f1b3d7a0
Create Date: 2026-07-05 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9f6a2c4e8b1"
down_revision: Union[str, Sequence[str], None] = "c8e5f1b3d7a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_translation_source",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_key", sa.String(length=255), nullable=False),
        sa.Column("lang", sa.String(length=5), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=False),
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
        sa.UniqueConstraint(
            "template_id", "content_key", "lang", name="uq_ai_translation_source_key"
        ),
    )
    op.create_index(
        op.f("ix_ai_translation_source_agency_id"), "ai_translation_source", ["agency_id"]
    )
    op.create_index(
        op.f("ix_ai_translation_source_template_id"), "ai_translation_source", ["template_id"]
    )
    op.execute("ALTER TABLE ai_translation_source ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_translation_source_template_id"), table_name="ai_translation_source")
    op.drop_index(op.f("ix_ai_translation_source_agency_id"), table_name="ai_translation_source")
    op.drop_table("ai_translation_source")
