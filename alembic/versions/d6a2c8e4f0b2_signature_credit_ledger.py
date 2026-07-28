"""Ledger de crédits signature (méga-lot 28/07, lot 2)

`signature_credit_balance` (une ligne par agence, CHECK >= 0 — l'invariant
« jamais négatif » au niveau DB) + `signature_credit_entry` (append-only,
ceinture d'idempotence unique sur paddle_event_id). Additif pur.

Revision ID: d6a2c8e4f0b2
Revises: c4f0a2e8d6b0
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d6a2c8e4f0b2"
down_revision = "c4f0a2e8d6b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signature_credit_balance",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agency_id", sa.Uuid(), nullable=False),
        sa.Column("available", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.CheckConstraint(
            "available >= 0", name="ck_signature_credit_balance_available_never_negative"
        ),
        sa.CheckConstraint(
            "reserved >= 0", name="ck_signature_credit_balance_reserved_never_negative"
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agency.id"],
            name=op.f("fk_signature_credit_balance_agency_id_agency"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signature_credit_balance")),
        sa.UniqueConstraint("agency_id", name="uq_signature_credit_balance_agency"),
    )
    op.create_index(
        op.f("ix_signature_credit_balance_agency_id"), "signature_credit_balance", ["agency_id"]
    )

    op.create_table(
        "signature_credit_entry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agency_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("signature_request_id", sa.Uuid(), nullable=True),
        sa.Column("paddle_event_id", sa.String(length=64), nullable=True),
        sa.Column("details", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agency.id"],
            name=op.f("fk_signature_credit_entry_agency_id_agency"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["signature_request_id"],
            ["signature_request.id"],
            name=op.f("fk_signature_credit_entry_signature_request_id_signature_request"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signature_credit_entry")),
        sa.UniqueConstraint("paddle_event_id", name="uq_signature_credit_entry_paddle_event"),
    )
    op.create_index(
        op.f("ix_signature_credit_entry_agency_id"), "signature_credit_entry", ["agency_id"]
    )
    op.create_index(
        op.f("ix_signature_credit_entry_signature_request_id"),
        "signature_credit_entry",
        ["signature_request_id"],
    )

    # Posture RLS de prod (garde test_rls) — deny-all sans policy.
    op.execute('ALTER TABLE "signature_credit_balance" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "signature_credit_entry" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table("signature_credit_entry")
    op.drop_table("signature_credit_balance")
