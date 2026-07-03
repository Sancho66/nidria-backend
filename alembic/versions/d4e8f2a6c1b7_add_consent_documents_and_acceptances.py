"""add consent_document + consent_acceptance (blocking clickwrap, point 16)

`consent_document`: versioned legal texts, UNIQUE(type, version); the gate
requires the latest ACTIVE version per type. `consent_acceptance`: the
immutable clickwrap trace (insert-only), bare-UUID actor/agency (no FK: the
legal trace survives account/agency deletion), UNIQUE NULLS NOT DISTINCT
under the manager's idempotence check (PG16).

Both tables get RLS enabled (deny-all for the Supabase REST roles), as
required for every table created after the a103249eb0a1 sweep.

Revision ID: d4e8f2a6c1b7
Revises: a103249eb0a1
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e8f2a6c1b7"
down_revision: Union[str, Sequence[str], None] = "a103249eb0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "consent_document",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.UniqueConstraint("type", "version", name="uq_consent_document_type_version"),
    )
    op.create_index(op.f("ix_consent_document_type"), "consent_document", ["type"], unique=False)

    op.create_table(
        "consent_acceptance",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_type", sa.String(length=10), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_type",
            "actor_id",
            "document_type",
            "document_version",
            "agency_id",
            name="uq_consent_acceptance",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_consent_acceptance_actor",
        "consent_acceptance",
        ["actor_type", "actor_id"],
        unique=False,
    )

    # RLS deny-all for the Supabase REST roles (owner stays exempt), same
    # policy as the a103249eb0a1 sweep for every pre-existing table.
    op.execute("ALTER TABLE consent_document ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE consent_acceptance ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_consent_acceptance_actor", table_name="consent_acceptance")
    op.drop_table("consent_acceptance")
    op.drop_index(op.f("ix_consent_document_type"), table_name="consent_document")
    op.drop_table("consent_document")
