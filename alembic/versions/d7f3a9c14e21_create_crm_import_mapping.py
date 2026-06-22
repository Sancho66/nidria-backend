"""create crm_import_mapping (BLOC 3 — saved CSV→parcours mappings)

Additive: a brand-new table, no existing column/row touched. Reversible: the
downgrade drops the table and its indexes cleanly (a full up→down→up roundtrip
leaves the schema identical). Scoped to an agency (agency_id NOT NULL, FK
CASCADE); the journey_template FK cascades too (a mapping is meaningless
without its parcours). UNIQUE(agency_id, journey_template_id, crm_slug) — one
natural mapping per (agency, parcours, CRM).

Revision ID: d7f3a9c14e21
Revises: c4e1a7b9d2f8
Create Date: 2026-06-22 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7f3a9c14e21"
down_revision: str | Sequence[str] | None = "c4e1a7b9d2f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crm_import_mapping",
        sa.Column("agency_id", sa.Uuid(), nullable=False),
        sa.Column("journey_template_id", sa.Uuid(), nullable=False),
        sa.Column("crm_slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("mapping", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
            name=op.f("fk_crm_import_mapping_agency_id_agency"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["journey_template_id"],
            ["journey_template.id"],
            name=op.f("fk_crm_import_mapping_journey_template_id_journey_template"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crm_import_mapping")),
        sa.UniqueConstraint(
            "agency_id", "journey_template_id", "crm_slug", name="uq_crm_import_mapping"
        ),
    )
    op.create_index(
        op.f("ix_crm_import_mapping_agency_id"),
        "crm_import_mapping",
        ["agency_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_crm_import_mapping_journey_template_id"),
        "crm_import_mapping",
        ["journey_template_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_crm_import_mapping_journey_template_id"), table_name="crm_import_mapping"
    )
    op.drop_index(op.f("ix_crm_import_mapping_agency_id"), table_name="crm_import_mapping")
    op.drop_table("crm_import_mapping")
