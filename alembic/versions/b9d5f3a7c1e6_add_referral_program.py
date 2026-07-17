"""add referral program (codes, attribution, credits ledger)

Referral (parrainage): agency.referral_code (dedicated shareable code —
NOT the guessable public slug), agency.referred_by_agency_id (attribution
typed at signup/wizard, immutable), and the referral_credit ledger (the
TRUTH the Paddle discount executes). Existing agencies are BACKFILLED
with a generated code. RLS enabled on the new table (deny-all posture,
same as every public table).

Revision ID: b9d5f3a7c1e6
Revises: a8c4e0f6b2d9
Create Date: 2026-07-17 15:00:00.000000

"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9d5f3a7c1e6"
down_revision: str | Sequence[str] | None = "a8c4e0f6b2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No ambiguous glyphs (0/O, 1/I/L) — the code gets typed by humans.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _generate_code() -> str:
    return "NID-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


def upgrade() -> None:
    op.add_column("agency", sa.Column("referral_code", sa.String(16), nullable=True))
    op.create_index("ix_agency_referral_code", "agency", ["referral_code"], unique=True)
    op.add_column(
        "agency",
        sa.Column(
            "referred_by_agency_id",
            sa.Uuid(),
            sa.ForeignKey("agency.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_table(
        "referral_credit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "referrer_agency_id",
            sa.Uuid(),
            sa.ForeignKey("agency.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "referred_agency_id",
            sa.Uuid(),
            sa.ForeignKey("agency.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate", sa.Integer(), nullable=False, server_default=sa.text("20")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("referred_agency_id", name="uq_referral_credit_referred"),
    )
    # Deny-all posture: every public table is born with RLS (guard test).
    op.execute("ALTER TABLE referral_credit ENABLE ROW LEVEL SECURITY")

    # Backfill: every existing agency gets its shareable code.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id FROM agency WHERE referral_code IS NULL")).all()
    taken: set[str] = set()
    for (agency_id,) in rows:
        code = _generate_code()
        while code in taken:
            code = _generate_code()
        taken.add(code)
        bind.execute(
            sa.text("UPDATE agency SET referral_code = :c WHERE id = :i").bindparams(
                c=code, i=agency_id
            )
        )


def downgrade() -> None:
    op.drop_table("referral_credit")
    op.drop_column("agency", "referred_by_agency_id")
    op.drop_index("ix_agency_referral_code", table_name="agency")
    op.drop_column("agency", "referral_code")
