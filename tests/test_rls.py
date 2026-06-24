"""Row-Level Security posture (deny-all) — proven on the testcontainer.

conftest mirrors prod by ENABLING RLS with NO policy on every table after
create_all (the same posture the a103249eb0a1 migration sets). These tests
prove the two halves of the security claim:

  1. coverage  — RLS is ON for EVERY public table (none forgotten);
  2. deny-all  — a non-bypass role (Supabase's anon, which DOES hold table
                 grants) is fully denied: SELECT returns nothing, INSERT is
                 rejected — while the OWNER role (the backend) is unaffected.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.plugins.agency_plugin import MakeAgency

# A non-login role WITHOUT bypassrls and WITHOUT table ownership — the exact
# privilege shape of Supabase's `anon` / `authenticated`. It is GRANTED table
# access on purpose, so any denial below comes from RLS, not from missing grants.
_PROBE = "rls_anon_probe"

# asyncpg's prepared-statement protocol rejects multi-command strings, so each
# statement is executed on its own. The DO block is itself one statement.
_DROP_PROBE = (
    f"DO $$ BEGIN "
    f"IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{_PROBE}') THEN "
    f"EXECUTE 'DROP OWNED BY {_PROBE}'; EXECUTE 'DROP ROLE {_PROBE}'; "
    f"END IF; END $$"
)
_SETUP = [
    _DROP_PROBE,
    f"CREATE ROLE {_PROBE} NOLOGIN NOBYPASSRLS",
    f"GRANT USAGE ON SCHEMA public TO {_PROBE}",
    f"GRANT SELECT, INSERT ON agency TO {_PROBE}",
]


async def test_rls_enabled_on_every_public_table(db_session: AsyncSession) -> None:
    """Coverage: not a single base table in `public` is left without RLS."""
    rows = await db_session.execute(
        text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "AND NOT c.relrowsecurity ORDER BY c.relname"
        )
    )
    without_rls = [r[0] for r in rows]
    assert without_rls == [], f"tables without RLS enabled: {without_rls}"


async def test_non_bypass_role_is_denied_all(
    async_engine: AsyncEngine, make_agency: MakeAgency
) -> None:
    """Deny-all: the anon-shaped role sees no rows and cannot write, while
    the owner (the backend's role) reads and writes normally."""
    await make_agency(slug="rls-visible-to-owner")  # committed, real row

    async with async_engine.begin() as conn:
        for stmt in _SETUP:
            await conn.execute(text(stmt))

    try:
        # --- under the non-bypass role: deny-all ---------------------------------
        async with async_engine.connect() as conn:
            await conn.execute(text(f"SET ROLE {_PROBE}"))

            seen = (await conn.execute(text("SELECT count(*) FROM agency"))).scalar_one()
            assert seen == 0, "RLS deny-all must hide every row from a non-bypass role"

            with pytest.raises(DBAPIError) as exc:
                await conn.execute(
                    text(
                        "INSERT INTO agency (id, name, slug, settings) VALUES "
                        "(gen_random_uuid(), 'probe', 'rls-probe-write', '{}'::jsonb)"
                    )
                )
            assert "row-level security" in str(exc.value).lower()
            await conn.rollback()

        # --- owner (no SET ROLE): app intact -------------------------------------
        async with async_engine.connect() as conn:
            owner_seen = (await conn.execute(text("SELECT count(*) FROM agency"))).scalar_one()
            assert owner_seen >= 1, "the table OWNER must bypass RLS (no FORCE) — app intact"
    finally:
        async with async_engine.begin() as conn:
            await conn.execute(text(_DROP_PROBE))
