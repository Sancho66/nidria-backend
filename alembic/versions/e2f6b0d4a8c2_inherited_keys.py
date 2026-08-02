"""Option B (hérité/saisi) — le marqueur `inherited_keys` sur case_person.

Les références remplies DEPUIS la fiche au prefill ; effacées à toute
écriture (agence ou client). '[]' par défaut : le stock existant est
« saisi » partout — honnête (verdict d'extraction : non rétrogradable).

Revision ID: e2f6b0d4a8c2
Revises: d0e4a8b2c6f0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2f6b0d4a8c2"
down_revision: str | None = "d0e4a8b2c6f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "case_person",
        sa.Column(
            "inherited_keys",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("case_person", "inherited_keys")
