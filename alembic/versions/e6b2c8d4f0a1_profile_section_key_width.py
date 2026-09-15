"""profile_section columns widened to the section key width (50)

Prod incident 15/09/2026 18:03 UTC (Fly-Request-Id 01M2K3SCZH4GCRY2C6V2KM5PPK):
POST /agencies/me/custom-fields on the agency section « Suivi Client /
Prospect » (key `suivi_client_prospect`, 21 chars) → asyncpg
StringDataRightTruncationError « value too long for type character
varying(20) » → 500. Since the agency-configurable sections lot
(462cb09, 07/08/2026) a section key may be 50 chars long
(`agency_profile_section.key` String(50), slug pattern {0,49}), while
the two columns that POINT at it stayed at the catalogue-era width of 20.
This aligns them: 50, the key's own width. Widening only — no data touched.

Revision ID: e6b2c8d4f0a1
Revises: d5a1b7c3e9f2
"""

import sqlalchemy as sa

from alembic import op

revision = "e6b2c8d4f0a1"
down_revision = "d5a1b7c3e9f2"
branch_labels = None
depends_on = None

_TABLES = ("custom_field_definition", "company_field_definition")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "profile_section",
            existing_type=sa.String(length=20),
            type_=sa.String(length=50),
            existing_nullable=False,
            existing_server_default="misc",
        )


def downgrade() -> None:
    # Only safe while no stored key exceeds 20 chars; the incident's own
    # section would refuse the shrink, which is the right outcome.
    for table in _TABLES:
        op.alter_column(
            table,
            "profile_section",
            existing_type=sa.String(length=50),
            type_=sa.String(length=20),
            existing_nullable=False,
            existing_server_default="misc",
        )
