"""add signup_verification (self-serve signup, code by email)

Stage 1: a 6-digit code (stored HASHED, 15-minute expiry, dies after 5
wrong attempts) proves the email BEFORE anything exists — no ghost
agency, ever. Stage 2: a long completion_token (30 min) authorizes the
final creation. One live verification per email (a re-request kills the
previous). RLS enabled (deny-all posture, like every public table).

Revision ID: c0e6a2d8f4b1
Revises: b9d5f3a7c1e6
Create Date: 2026-07-17 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0e6a2d8f4b1"
down_revision: str | Sequence[str] | None = "b9d5f3a7c1e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signup_verification",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, index=True),
        sa.Column("lang", sa.String(2), nullable=False, server_default=sa.text("'fr'")),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completion_token", sa.String(64), nullable=True),
        sa.Column("completion_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("completion_token", name="uq_signup_verification_completion_token"),
    )
    op.execute("ALTER TABLE signup_verification ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("signup_verification")
