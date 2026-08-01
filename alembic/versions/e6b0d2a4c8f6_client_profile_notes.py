"""Complément sections — notes de fiche client.

`client_profile_note` : le miroir strict de `case_note` porté par la
fiche (profile_id CASCADE, auteur SET NULL, body, is_confidential).
Additive ; les notes de dossier ne bougent pas.

Revision ID: e6b0d2a4c8f6
Revises: d4a8b2c6e0f2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e6b0d2a4c8f6"
down_revision: str | None = "d4a8b2c6e0f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_profile_note",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("client_profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_confidential", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.create_index("ix_client_profile_note_profile_id", "client_profile_note", ["profile_id"])
    op.execute('ALTER TABLE "client_profile_note" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_index("ix_client_profile_note_profile_id", table_name="client_profile_note")
    op.drop_table("client_profile_note")
