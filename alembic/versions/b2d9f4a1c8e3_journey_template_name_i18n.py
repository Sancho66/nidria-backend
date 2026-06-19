"""journey_template.name_i18n — i18n blob for the template name (complement to
the BLOC 1 five fields, which omitted it)

Additive and reversible, same pattern as the BLOC 1 i18n blobs: a parallel
{lang: text} JSONB next to the scalar `name`. The scalar stays the read
fallback AND the seed's idempotence anchor (the seed keys on the scalar, never
the blob). Backfill seeds the FR variant from the current name. Downgrade drops
the column; the scalar is never altered.

Revision ID: b2d9f4a1c8e3
Revises: a1c7e2f9b4d6
Create Date: 2026-06-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2d9f4a1c8e3"
down_revision: str | Sequence[str] | None = "a1c7e2f9b4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "journey_template",
        sa.Column(
            "name_i18n", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    # Seed the FR variant from the current scalar name (name is NOT NULL).
    op.execute("UPDATE journey_template SET name_i18n = jsonb_build_object('fr', name)")


def downgrade() -> None:
    op.drop_column("journey_template", "name_i18n")
