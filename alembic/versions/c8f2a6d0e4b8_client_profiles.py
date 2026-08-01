"""Chantier fiches F1 — client_profile + scope + liaison + BACKFILL.

- `client_profile` : la fiche d'agence (agency_id + expat_user_id UNIQUE,
  miroir civil de case_person + custom_fields + source/tags propres), RLS.
- `custom_field_definition.scope` ('person'|'case', défaut 'case') —
  backfillée 'person' pour les 21 clés du catalogue classées Phase 0
  (sections identity/contact/family_situation/company).
- `case_person.client_profile_id` nullable SET NULL — la SEULE
  modification de case_person (invariants Phase 0).
- Backfill : une fiche par (agence, expat), latest-wins par champ,
  liaison des lignes — idempotent, rejouable (src/client_profiles/backfill).

Revision ID: c8f2a6d0e4b8
Revises: b6e0c4a8d2f6
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c8f2a6d0e4b8"
down_revision = "b6e0c4a8d2f6"
branch_labels = None
depends_on = None

# Classification Phase 0 (sections identity/contact/family_situation/
# company du catalogue) — figée ICI : une migration ne lit jamais le code
# vivant du catalogue (il bouge, elle non).
PERSON_SCOPE_KEYS = (
    "birth_country",
    "second_nationality",
    "residence_address",
    "secondary_email",
    "whatsapp",
    "preferred_language",
    "preferred_contact_channel",
    "spouse_name",
    "spouse_nationality",
    "spouse_birth_date",
    "children_count",
    "dependents",
    "matrimonial_regime",
    "company_name",
    "legal_form",
    "share_capital",
    "company_registration_number",
    "registration_date",
    "headquarters_address",
    "legal_representative_name",
    "partners_count",
)


def upgrade() -> None:
    op.create_table(
        "client_profile",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agency_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agency.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "expat_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("expat_user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("passport_number", sa.String(length=50)),
        sa.Column("date_of_birth", sa.Date()),
        sa.Column("nationality", sa.String(length=100)),
        sa.Column("place_of_birth", sa.String(length=200)),
        sa.Column("sex", sa.String(length=1)),
        sa.Column("marital_status", sa.String(length=20)),
        sa.Column("phone", sa.String(length=50)),
        sa.Column("birth_name", sa.String(length=200)),
        sa.Column("profession", sa.String(length=200)),
        sa.Column("employer", sa.String(length=200)),
        sa.Column(
            "preferred_channels", postgresql.JSONB(), server_default="[]", nullable=False
        ),
        sa.Column(
            "custom_fields", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("source", sa.String(length=100)),
        sa.Column("tags", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.UniqueConstraint("agency_id", "expat_user_id", name="uq_client_profile_agency_expat"),
    )
    op.create_index("ix_client_profile_agency_id", "client_profile", ["agency_id"])
    op.create_index("ix_client_profile_expat_user_id", "client_profile", ["expat_user_id"])
    op.execute('ALTER TABLE "client_profile" ENABLE ROW LEVEL SECURITY')

    op.add_column(
        "custom_field_definition",
        sa.Column("scope", sa.String(length=10), server_default="case", nullable=False),
    )
    keys = ", ".join(f"'{k}'" for k in PERSON_SCOPE_KEYS)
    op.execute(f"UPDATE custom_field_definition SET scope = 'person' WHERE key IN ({keys})")

    op.add_column(
        "case_person",
        sa.Column("client_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_case_person_client_profile",
        "case_person",
        "client_profile",
        ["client_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_case_person_client_profile_id", "case_person", ["client_profile_id"])

    from src.client_profiles.backfill import backfill_client_profiles

    stats = backfill_client_profiles(op.get_bind())
    print(f"client_profile backfill: {stats}")


def downgrade() -> None:
    op.drop_index("ix_case_person_client_profile_id", table_name="case_person")
    op.drop_constraint("fk_case_person_client_profile", "case_person", type_="foreignkey")
    op.drop_column("case_person", "client_profile_id")
    op.drop_column("custom_field_definition", "scope")
    op.drop_index("ix_client_profile_expat_user_id", table_name="client_profile")
    op.drop_index("ix_client_profile_agency_id", table_name="client_profile")
    op.drop_table("client_profile")
