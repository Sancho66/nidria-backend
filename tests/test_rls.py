"""Row-Level Security posture (deny-all) — proven on the testcontainer.

conftest mirrors prod by ENABLING RLS with NO policy on every table after
create_all (the same posture the a103249eb0a1 migration sets). These tests
prove the two halves of the security claim:

  1. coverage  — RLS is ON for EVERY public table (none forgotten);
  2. deny-all  — a non-bypass role (Supabase's anon, which DOES hold table
                 grants) is fully denied: SELECT returns nothing, INSERT is
                 rejected — while the OWNER role (the backend) is unaffected.
"""

import re
from pathlib import Path

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


# --- structural guard: every NEW table must be born with RLS ---------------------------
#
# The two tests above prove the POSTURE, but on the testcontainer that posture
# comes from conftest (blanket ENABLE after create_all) — they can never catch
# a MIGRATION that creates a table without RLS. This scan constrains the
# migration files themselves: after the LAST dynamic sweep, any migration that
# creates a table must enable RLS on it in the SAME file. This is exactly how
# case_step_cost, journey_step_cost and paddle_webhook_event slipped through
# (created after sweep a103249eb0a1, closed by sweep c4d8e2f6a1b3).

_VERSIONS_DIR = Path("alembic/versions")


def _revision_chain() -> list[tuple[str, str]]:
    """The linear (revision, file text) chain, root → head."""
    by_rev: dict[str, tuple[str | None, str]] = {}
    for path in _VERSIONS_DIR.glob("*.py"):
        body = path.read_text(encoding="utf-8")
        rev_m = re.search(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']", body, re.M)
        down_m = re.search(r"^down_revision(?:[^=]+)?=\s*(.+)$", body, re.M)
        assert rev_m and down_m, f"unparseable migration header: {path.name}"
        down_raw = down_m.group(1).strip()
        down = None if down_raw == "None" else down_raw.strip("\"'")
        by_rev[rev_m.group(1)] = (down, body)
    child_of = {down: rev for rev, (down, _) in by_rev.items()}
    chain: list[tuple[str, str]] = []
    cursor = child_of.get(None)
    while cursor is not None:
        chain.append((cursor, by_rev[cursor][1]))
        cursor = child_of.get(cursor)
    assert len(chain) == len(by_rev), "alembic chain is not linear or has orphans"
    return chain


def _is_sweep(body: str) -> bool:
    """A dynamic sweep enumerates pg_tables and enables RLS on everything."""
    return "FROM pg_tables" in body and "ENABLE ROW LEVEL SECURITY" in body


def test_every_table_creating_migration_enables_rls() -> None:
    chain = _revision_chain()
    last_sweep = max(i for i, (_, body) in enumerate(chain) if _is_sweep(body))
    naked: list[str] = []
    for rev, body in chain[last_sweep + 1 :]:
        for table in re.findall(r"create_table\(\s*[\"']([^\"']+)[\"']", body):
            enable = re.compile(
                rf"ALTER TABLE\s+(?:public\.)?\"?{re.escape(table)}\"?\s+"
                rf"ENABLE ROW LEVEL SECURITY"
            )
            if not enable.search(body):
                naked.append(f"{table} (revision {rev})")
    assert naked == [], (
        "tables created WITHOUT enabling RLS — add "
        "op.execute('ALTER TABLE <table> ENABLE ROW LEVEL SECURITY') to the "
        f"creating migration (Supabase PostgREST exposes naked tables): {naked}"
    )
