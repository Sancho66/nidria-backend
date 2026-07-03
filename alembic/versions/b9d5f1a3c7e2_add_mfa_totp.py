"""add mfa_totp + mfa_backup_code + mfa_challenge (2FA TOTP, bloc 2)

Optional per-user TOTP (RFC 6238): enrollment row (pending until a first
valid code), bcrypt-hashed one-time backup codes, and the server-side
attempts counter of the login step-2 challenge. RLS enabled on all three
(post-sweep rule). Additive, cleanly reversible.

Revision ID: b9d5f1a3c7e2
Revises: a8c4e2f6b0d3
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9d5f1a3c7e2"
down_revision: Union[str, Sequence[str], None] = "a8c4e2f6b0d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mfa_totp",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_type", sa.String(length=10), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("secret", sa.String(length=64), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_type", "actor_id", name="uq_mfa_totp_actor"),
    )
    op.create_table(
        "mfa_backup_code",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mfa_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["mfa_id"], ["mfa_totp.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mfa_backup_code_mfa_id"), "mfa_backup_code", ["mfa_id"])
    op.create_table(
        "mfa_challenge",
        sa.Column("jti", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_type", sa.String(length=10), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("jti"),
    )
    op.create_index("ix_mfa_challenge_actor", "mfa_challenge", ["actor_type", "actor_id"])
    for table in ("mfa_totp", "mfa_backup_code", "mfa_challenge"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_mfa_challenge_actor", table_name="mfa_challenge")
    op.drop_table("mfa_challenge")
    op.drop_index(op.f("ix_mfa_backup_code_mfa_id"), table_name="mfa_backup_code")
    op.drop_table("mfa_backup_code")
    op.drop_table("mfa_totp")
