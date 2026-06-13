"""drop legacy required_documents on journey_template_step

The legacy free-label document list (step 15) is superseded by document
requirements (step_requirement / case_step_requirement), which are tracked
and linked to an actual file. Drop the legacy column. Data loss is accepted
(legacy, replaced). Downgrade re-adds it as a nullable JSONB (symmetric).

Revision ID: b7f3c1a9d2e4
Revises: 82e355266ed5
Create Date: 2026-06-13 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7f3c1a9d2e4"
down_revision: Union[str, Sequence[str], None] = "82e355266ed5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("journey_template_step", "required_documents")


def downgrade() -> None:
    op.add_column(
        "journey_template_step",
        sa.Column(
            "required_documents",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
