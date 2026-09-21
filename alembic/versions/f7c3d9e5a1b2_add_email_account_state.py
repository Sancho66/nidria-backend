"""Shared Resend usage observations and durable alert deduplication."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f7c3d9e5a1b2"
down_revision = "e6b2c8d4f0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_account_state",
        sa.Column("provider", sa.String(30), primary_key=True),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute("ALTER TABLE email_account_state ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("email_account_state")
