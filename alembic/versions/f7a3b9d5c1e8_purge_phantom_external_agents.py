"""purge phantom external agents (dead pre-created invitation accounts)

The external-invitation flow PRE-CREATES the Agent row at invite time
(honest directory state). Until the invitation-hygiene lot, cancelling
(or letting expire) such an invitation left that agent behind — a
phantom with a throwaway password that kept counting in the provider
gate. The runtime now purges on cancellation; this migration purges the
phantoms that already exist: external agents whose directory contact has
NO live claim (no accepted invitation, no pending non-expired one).
Found at authoring time: 1 in prod (a test contact), 2 locally.

Irreversible by nature (the phantoms carry no data worth keeping):
downgrade is a no-op.

Revision ID: f7a3b9d5c1e8
Revises: e6f2a8c4d0b3
Create Date: 2026-07-17 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a3b9d5c1e8"
down_revision: str | Sequence[str] | None = "e6f2a8c4d0b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PHANTOMS = """
    SELECT a.id AS agent_id, ec.id AS contact_id
    FROM agent a
    JOIN external_contact ec ON ec.agent_id = a.id
    WHERE a.is_external
      AND NOT EXISTS (
        SELECT 1 FROM agent_invitation x
        WHERE x.external_contact_id = ec.id
          AND (
            x.status = 'accepted'
            OR (x.status = 'pending' AND x.expires_at > now())
          )
      )
"""


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(_PHANTOMS)).all()
    for agent_id, contact_id in rows:
        # Unlink first (the contact returns to 'none', re-invitable),
        # then drop the never-accepted account.
        bind.execute(
            sa.text("UPDATE external_contact SET agent_id = NULL WHERE id = :c").bindparams(
                c=contact_id
            )
        )
        bind.execute(sa.text("DELETE FROM agent WHERE id = :a").bindparams(a=agent_id))


def downgrade() -> None:
    pass  # nothing to restore: the phantoms carried no reachable account
