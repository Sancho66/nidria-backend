"""lowercase identity emails (agent, expat_user, invitations)

Account emails are now normalized (trim + lowercase) at every write and
lookup boundary (NormalizedEmailStr / normalize_email); this migration
brings the EXISTING rows to the same canonical form so exact matching
holds. Prod incident: an account created 'Contact@x' was unreachable by
forgot-password typed 'contact@x' (silent 200, no mail).

Safety: on the two ACCOUNT tables (agent, expat_user — email UNIQUE), if
two rows differ only by case (distinct casings collapsing to one value),
the migration ABORTS loudly: merging accounts is a human decision, never
an implicit UPDATE. The invitation tables have NO email uniqueness
(several invitations for one email are normal: one per case, re-sends),
so they are lowercased unconditionally — nothing can collide.

Downgrade is a data no-op (the original casing is not kept anywhere);
the schema is untouched in both directions.

Revision ID: e5f1a7c3b9d2
Revises: d4e8f2a6c1b7
Create Date: 2026-07-03 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f1a7c3b9d2"
down_revision: Union[str, Sequence[str], None] = "d4e8f2a6c1b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Identity tables only: contact cards (external_contact) keep their
# casing, they are display/notification data and never matched on.
_UNIQUE_EMAIL_TABLES = ("agent", "expat_user")
_TABLES = (*_UNIQUE_EMAIL_TABLES, "agent_invitation", "case_invitation")


def upgrade() -> None:
    bind = op.get_bind()
    # Guard only where email is UNIQUE: distinct casings of one address
    # would collapse into a constraint violation. count(DISTINCT email)
    # — plain duplicates (same casing) are a different, legal shape on
    # non-unique tables and never our business here.
    for table in _UNIQUE_EMAIL_TABLES:
        collisions = bind.execute(
            sa.text(
                f"SELECT lower(email) FROM {table}"  # noqa: S608 — table names are our literals
                " GROUP BY lower(email) HAVING count(DISTINCT email) > 1"
            )
        ).all()
        if collisions:
            values = sorted(row[0] for row in collisions)
            raise RuntimeError(
                f"{table}: rows differing only by email case would collapse: {values}."
                " Merge or delete the duplicates manually, then re-run."
            )
    for table in _TABLES:
        op.execute(
            f"UPDATE {table} SET email = lower(btrim(email)) WHERE email <> lower(btrim(email))"
        )


def downgrade() -> None:
    # Data no-op: the original casing is gone by design (canonical form).
    pass
