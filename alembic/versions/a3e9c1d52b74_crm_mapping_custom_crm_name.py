"""crm_import_mapping: add custom_crm_name (Autre / CRM générique)

Additive: a single nullable column for the free CRM label used by custom
(unreferenced) imports — crm_slug="custom". NULL for referenced CRMs. No
existing row/column touched, no constraint change. Reversible: the downgrade
drops the column (clean roundtrip).

Revision ID: a3e9c1d52b74
Revises: f1a2b3c4d5e6
Create Date: 2026-06-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3e9c1d52b74"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crm_import_mapping",
        sa.Column("custom_crm_name", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crm_import_mapping", "custom_crm_name")
