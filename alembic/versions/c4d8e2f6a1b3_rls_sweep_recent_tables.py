"""RLS sweep #2 — close the tables born naked since the first sweep

Supabase alert: three tables created AFTER the a103249eb0a1 sweep forgot to
enable RLS in their own migration (case_step_cost, journey_step_cost,
paddle_webhook_event), leaving them readable/writable through Supabase's
auto-generated PostgREST API with the anon key. Same fix, same rationale as
the first sweep: RLS ENABLED + ZERO policies = deny-all for the RLS-subject
roles; the backend connects as `postgres` (table owner AND bypassrls) so
the application is untouched — only the direct REST/anon path closes.

The sweep is DYNAMIC (every public table, from pg_tables) and idempotent,
so it also covers any table this enumeration finds beyond the three known.

Recurrence guard: tests/test_rls.py now carries a STRUCTURAL test — every
migration after the LAST sweep that creates a table must enable RLS on it
in the same file, or the suite fails. This sweep should be the last one.

Revision ID: c4d8e2f6a1b3
Revises: e6f1a8c4d2b7
Create Date: 2026-07-14 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d8e2f6a1b3"
down_revision: str | Sequence[str] | None = "e6f1a8c4d2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _public_tables() -> list[str]:
    """Every base table in the public schema, at migration time."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    )
    return [row[0] for row in rows]


def upgrade() -> None:
    for table in _public_tables():
        # Identifier is a pg_tables value (not user input); quote it anyway.
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    # Disable ONLY what this sweep newly closed: the tables the first sweep
    # (a103249eb0a1) covered keep their RLS — a full-set disable here would
    # silently undo migration a103249eb0a1's work on a mere downgrade of
    # this one.
    for table in ("case_step_cost", "journey_step_cost", "paddle_webhook_event"):
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
