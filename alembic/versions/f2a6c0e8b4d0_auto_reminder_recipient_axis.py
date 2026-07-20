"""widen the auto-reminder idempotence to the recipient axis (P2:
provider auto follow-ups)

The old UNIQUE (step_progress_id, auto_threshold_days) allowed ONE row
per step and threshold — the client one. Providers join the pass: the
belt becomes (step, threshold, recipient_type, provider) via a partial
unique index (COALESCE folds the client row's NULL external id).

Revision ID: f2a6c0e8b4d0
Revises: e8d4f0a6c2b8
Create Date: 2026-07-20 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a6c0e8b4d0"
down_revision: str | Sequence[str] | None = "e8d4f0a6c2b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_reminder_step_progress_id", "reminder")
    # No recipient_type in the key: an escalated line (external -> agent,
    # provenance kept) must keep blocking its (step, threshold, provider).
    op.execute(
        "CREATE UNIQUE INDEX uq_reminder_auto_idempotence ON reminder "
        "(step_progress_id, auto_threshold_days, "
        "COALESCE(recipient_external_id, '00000000-0000-0000-0000-000000000000')) "
        "WHERE auto_threshold_days IS NOT NULL"
    )
    # The escalation provenance: 'agent' rows may now carry the external FK.
    op.drop_constraint("recipient_type_matches_fk", "reminder", type_="check")
    op.create_check_constraint(
        "recipient_type_matches_fk",
        "reminder",
        "(recipient_type = 'expat' AND recipient_external_id IS NULL)"
        " OR (recipient_type = 'external' AND recipient_external_id IS NOT NULL)"
        " OR (recipient_type = 'agent')",
    )


def downgrade() -> None:
    op.drop_constraint("recipient_type_matches_fk", "reminder", type_="check")
    op.create_check_constraint(
        "recipient_type_matches_fk",
        "reminder",
        "(recipient_type = 'expat' AND recipient_external_id IS NULL)"
        " OR (recipient_type = 'external' AND recipient_external_id IS NOT NULL)"
        " OR (recipient_type = 'agent' AND recipient_external_id IS NULL)",
    )
    op.execute("DROP INDEX uq_reminder_auto_idempotence")
    op.create_unique_constraint(
        "uq_reminder_step_progress_id",
        "reminder",
        ["step_progress_id", "auto_threshold_days"],
    )
