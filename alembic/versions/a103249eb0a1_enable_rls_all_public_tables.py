"""enable RLS (deny-all) on every public table

Security: close Supabase's auto-generated public REST API. RLS ENABLED +
ZERO policies = deny-all for the RLS-subject roles (anon / authenticated):
a SELECT returns no rows, a write is rejected. The backend connects as the
table OWNER — the SAME role Alembic uses to create the tables — and the
owner is EXEMPT from RLS unless FORCE ROW LEVEL SECURITY is set. We
deliberately do NOT set FORCE, so the application keeps full read/write
access and nothing breaks; only the direct REST/anon path is closed.

Coverage: EVERY table in the `public` schema, enumerated at runtime from
pg_tables (incl. tables not declared in the ORM metadata, and
alembic_version) so none is forgotten. Idempotent: ENABLE / DISABLE ROW
LEVEL SECURITY are no-ops when the table is already in that state, so the
sweep is safe to re-run. Reversible: downgrade DISABLES RLS on the same
set (clean roundtrip).

⚠️ FUTURE TABLES: a migration that runs once does NOT cover tables created
by LATER migrations. Any new table must enable RLS too — add
`op.execute('ALTER TABLE <t> ENABLE ROW LEVEL SECURITY')` to the migration
that creates it (or re-run this sweep). The durable guardrail is a
boot/CI check asserting pg_class.relrowsecurity on every public table.

Revision ID: a103249eb0a1
Revises: a3e9c1d52b74
Create Date: 2026-06-24 15:33:09.355304

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a103249eb0a1"
down_revision: str | Sequence[str] | None = "a3e9c1d52b74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _public_tables() -> list[str]:
    """Every base table in the public schema, at migration time."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    )
    return [row[0] for row in rows]


def _set_rls(enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    for table in _public_tables():
        # Identifier is a pg_tables value (not user input); quote it anyway.
        op.execute(f'ALTER TABLE public."{table}" {action} ROW LEVEL SECURITY')


def upgrade() -> None:
    _set_rls(enabled=True)


def downgrade() -> None:
    _set_rls(enabled=False)
