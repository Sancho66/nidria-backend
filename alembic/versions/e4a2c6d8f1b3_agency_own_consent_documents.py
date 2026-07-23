"""agency-owned consent documents (client terms override)

An agency may publish its OWN client terms, shown to ITS clients in place
of the Nidria text. Two additive nullable columns, NULL meaning exactly
what was true before:

- consent_document.agency_id — NULL = the canonical Nidria text (every
  existing row).
- consent_acceptance.document_agency_id — NULL = "signed the canonical
  text", which is what every existing acceptance did. NOT backfilled: the
  trace is insert-only and must stay evidentiary.

Both unique constraints are widened to carry the new column, with
NULLS NOT DISTINCT (PG16) so NULL keeps behaving as a real value — the
belt under the manager's idempotence check. Versions are numbered per
document owner, so an agency's v1 and Nidria's v1 coexist; without
document_agency_id in the acceptance key, an accepted canonical v1 would
silently satisfy an agency's brand-new v1.

Revision ID: e4a2c6d8f1b3
Revises: c3f9a1e7b5d2
"""

import sqlalchemy as sa
from alembic import op

revision = "e4a2c6d8f1b3"
down_revision = "c3f9a1e7b5d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("consent_document", sa.Column("agency_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_consent_document_agency_id", "consent_document", ["agency_id"], unique=False
    )
    op.drop_constraint("uq_consent_document_type_version", "consent_document", type_="unique")
    op.create_unique_constraint(
        "uq_consent_document_type_version",
        "consent_document",
        ["type", "version", "agency_id"],
        postgresql_nulls_not_distinct=True,
    )

    op.add_column("consent_acceptance", sa.Column("document_agency_id", sa.Uuid(), nullable=True))
    op.drop_constraint("uq_consent_acceptance", "consent_acceptance", type_="unique")
    op.create_unique_constraint(
        "uq_consent_acceptance",
        "consent_acceptance",
        [
            "actor_type",
            "actor_id",
            "document_type",
            "document_version",
            "agency_id",
            "document_agency_id",
        ],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_constraint("uq_consent_acceptance", "consent_acceptance", type_="unique")
    op.create_unique_constraint(
        "uq_consent_acceptance",
        "consent_acceptance",
        ["actor_type", "actor_id", "document_type", "document_version", "agency_id"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_column("consent_acceptance", "document_agency_id")

    op.drop_constraint("uq_consent_document_type_version", "consent_document", type_="unique")
    op.create_unique_constraint(
        "uq_consent_document_type_version", "consent_document", ["type", "version"]
    )
    op.drop_index("ix_consent_document_agency_id", table_name="consent_document")
    op.drop_column("consent_document", "agency_id")
