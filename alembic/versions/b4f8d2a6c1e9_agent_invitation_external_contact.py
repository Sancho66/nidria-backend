"""agent_invitation.external_contact_id — link a provider invitation to the
directory external_contact created at invite time (agent_id set on accept).

Additive, cleanly reversible.

Revision ID: b4f8d2a6c1e9
Revises: f2c9a1d5e7b3
Create Date: 2026-07-09 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4f8d2a6c1e9"
down_revision: Union[str, Sequence[str], None] = "f2c9a1d5e7b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_invitation",
        sa.Column("external_contact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_invitation_external_contact",
        "agent_invitation",
        "external_contact",
        ["external_contact_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_agent_invitation_external_contact", "agent_invitation", type_="foreignkey"
    )
    op.drop_column("agent_invitation", "external_contact_id")
