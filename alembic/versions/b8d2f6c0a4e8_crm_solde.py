"""Solde du chantier CRM (méga-lot V1-V4) — migration consolidée.

V1b : client_profile.status_override (nullable — l'agence prime sur la
dérivation quand posé).
V2a : case_person.relationship_kind (rôle canonique énuméré) + migration
DOUCE des textes libres constatés en prod (spouse/child/partner/manager,
mapping au rapport) — `relationship` reste le libellé.
V2b : company_profile + company_profile_role (RLS), la fiche société.
V2c : client_case.company_profile_id (nullable SET NULL — le dossier
concerne une société).
V4b : crm_import_mapping.journey_template_id NULLABLE (config d'agence,
import-fiches) + unicité NULLS NOT DISTINCT (deux configs d'agence
homonymes restent un conflit). Constat : les configs existantes gardent
leur parcours — aucune donnée touchée.

Rejouable ; downgrade complet.

Revision ID: b8d2f6c0a4e8
Revises: a4c8e2f6b0d4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8d2f6c0a4e8"
down_revision: str | None = "a4c8e2f6b0d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# V2a — la migration douce des textes libres constatés (casse/accents
# ignorés, préfixe suffit pour « gerant manager »).
RELATIONSHIP_KIND_PATTERNS = (
    ("spouse", ("spouse", "wife", "husband", "conjoint", "epouse", "époux", "epoux", "mari")),
    ("child", ("child", "son", "daughter", "enfant", "fils", "fille")),
    ("partner", ("associé", "associe", "associee", "associée", "partner")),
    ("manager", ("gérant", "gerant", "manager")),
)


def upgrade() -> None:
    # V1b
    op.add_column("client_profile", sa.Column("status_override", sa.String(length=10)))

    # V2a
    op.add_column("case_person", sa.Column("relationship_kind", sa.String(length=20)))
    for kind, needles in RELATIONSHIP_KIND_PATTERNS:
        for needle in needles:
            op.execute(
                sa.text(
                    "UPDATE case_person SET relationship_kind = :kind "
                    "WHERE relationship_kind IS NULL "
                    "AND lower(relationship) LIKE :needle || '%'"
                ).bindparams(kind=kind, needle=needle)
            )
    op.execute(
        "UPDATE case_person SET relationship_kind = 'other' "
        "WHERE relationship_kind IS NULL AND relationship IS NOT NULL"
    )

    # V2b
    op.create_table(
        "company_profile",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agency.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "custom_fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.create_index("ix_company_profile_agency_id", "company_profile", ["agency_id"])
    op.execute('ALTER TABLE "company_profile" ENABLE ROW LEVEL SECURITY')
    op.create_table(
        "company_profile_role",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("company_profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "client_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("client_profile.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("role_label", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.UniqueConstraint(
            "company_profile_id", "client_profile_id", "role", name="uq_company_profile_role"
        ),
    )
    op.create_index(
        "ix_company_profile_role_company", "company_profile_role", ["company_profile_id"]
    )
    op.create_index(
        "ix_company_profile_role_person", "company_profile_role", ["client_profile_id"]
    )
    op.execute('ALTER TABLE "company_profile_role" ENABLE ROW LEVEL SECURITY')

    # V2c
    op.add_column(
        "client_case",
        sa.Column(
            "company_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("company_profile.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_client_case_company_profile_id", "client_case", ["company_profile_id"])

    # V4b
    op.alter_column("crm_import_mapping", "journey_template_id", nullable=True)
    op.drop_constraint("uq_crm_import_mapping", "crm_import_mapping", type_="unique")
    op.execute(
        "ALTER TABLE crm_import_mapping ADD CONSTRAINT uq_crm_import_mapping "
        "UNIQUE NULLS NOT DISTINCT (agency_id, journey_template_id, crm_slug, name)"
    )


def downgrade() -> None:
    op.drop_constraint("uq_crm_import_mapping", "crm_import_mapping", type_="unique")
    op.execute("DELETE FROM crm_import_mapping WHERE journey_template_id IS NULL")
    op.alter_column("crm_import_mapping", "journey_template_id", nullable=False)
    op.create_unique_constraint(
        "uq_crm_import_mapping",
        "crm_import_mapping",
        ["agency_id", "journey_template_id", "crm_slug", "name"],
    )
    op.drop_index("ix_client_case_company_profile_id", table_name="client_case")
    op.drop_column("client_case", "company_profile_id")
    op.drop_index("ix_company_profile_role_person", table_name="company_profile_role")
    op.drop_index("ix_company_profile_role_company", table_name="company_profile_role")
    op.drop_table("company_profile_role")
    op.drop_index("ix_company_profile_agency_id", table_name="company_profile")
    op.drop_table("company_profile")
    op.drop_column("case_person", "relationship_kind")
    op.drop_column("client_profile", "status_override")
