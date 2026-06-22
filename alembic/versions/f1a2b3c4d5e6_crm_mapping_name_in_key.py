"""crm_import_mapping: add `name` to the unique key (multi-config per CRM)

The mapping key becomes UNIQUE(agency_id, journey_template_id, crm_slug, name)
and `name` becomes NOT NULL — so SEVERAL named configs coexist for one
(agency, parcours, CRM): two different names = two rows, a same-name create =
409. Existing NULL names are backfilled to a non-empty default first (at most
one per (parcours, CRM) under the old 3-col key, so no collision).

Reversible: the downgrade restores the 3-col key + nullable `name`. A full
up→down→up roundtrip on an empty schema is identical (the roundtrip test
proves it); going down with REAL multi-config data would violate the 3-col
unique — that is the intended one-way nature of the data, not a migration bug.

Revision ID: f1a2b3c4d5e6
Revises: d7f3a9c14e21
Create Date: 2026-06-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "d7f3a9c14e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE crm_import_mapping SET name = 'Import' WHERE name IS NULL")
    op.alter_column(
        "crm_import_mapping",
        "name",
        existing_type=sa.String(length=200),
        nullable=False,
    )
    op.drop_constraint("uq_crm_import_mapping", "crm_import_mapping", type_="unique")
    op.create_unique_constraint(
        "uq_crm_import_mapping",
        "crm_import_mapping",
        ["agency_id", "journey_template_id", "crm_slug", "name"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_crm_import_mapping", "crm_import_mapping", type_="unique")
    op.create_unique_constraint(
        "uq_crm_import_mapping",
        "crm_import_mapping",
        ["agency_id", "journey_template_id", "crm_slug"],
    )
    op.alter_column(
        "crm_import_mapping",
        "name",
        existing_type=sa.String(length=200),
        nullable=True,
    )
