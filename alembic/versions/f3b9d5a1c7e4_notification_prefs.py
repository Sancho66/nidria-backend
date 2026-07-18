"""notification preferences (agency client prefs + agent prefs)

Additive: agent.notification_prefs (JSONB, NULL = defaults). Soft
migration of the legacy step_notifications_enabled flag: false becomes
client prefs all-notifications-off (requirement_request/comments off,
progress_digest off — reminders stay ON: the flag never governed them,
converting behaviour is not the mandate); the key is then REMOVED (read
nowhere after this lot). true/absent = the defaults (no key written).

Revision ID: f3b9d5a1c7e4
Revises: e2a8c4f0b6d3
Create Date: 2026-07-18 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3b9d5a1c7e4"
down_revision: str | Sequence[str] | None = "e2a8c4f0b6d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent", sa.Column("notification_prefs", JSONB(), nullable=True))
    # Soft migration: the agencies that had explicitly muted step
    # notifications keep their silence, expressed in the new model.
    op.execute(
        """
        UPDATE agency
        SET settings = (settings - 'step_notifications_enabled')
            || jsonb_build_object(
                'notification_prefs',
                jsonb_build_object(
                    'client',
                    jsonb_build_object(
                        'requirement_request', 'off',
                        'comments', 'off',
                        'progress_digest', 'off'
                    )
                )
            )
        WHERE settings ? 'step_notifications_enabled'
          AND (settings ->> 'step_notifications_enabled')::boolean IS FALSE
        """
    )
    # true (explicit) = the defaults: just drop the legacy key.
    op.execute(
        """
        UPDATE agency SET settings = settings - 'step_notifications_enabled'
        WHERE settings ? 'step_notifications_enabled'
        """
    )


def downgrade() -> None:
    op.drop_column("agent", "notification_prefs")
