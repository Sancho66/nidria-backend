"""add ai_translation_job.failed_keys (done_with_gaps: residual keys to review)

Additive. NOT NULL JSONB default '[]'. Existing rows become gap-free.
The residual keys of a done_with_gaps job (e.g. an RU field the model
could not render in Cyrillic even after the repair pass) are exposed
here for manual review; the job no longer fails on such a residue.

Revision ID: b4e0c2a8d6f2
Revises: a2c8e4f0b6d2
Create Date: 2026-07-21 10:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e0c2a8d6f2"
down_revision: Union[str, Sequence[str], None] = "a2c8e4f0b6d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_translation_job",
        sa.Column(
            "failed_keys",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_translation_job", "failed_keys")
