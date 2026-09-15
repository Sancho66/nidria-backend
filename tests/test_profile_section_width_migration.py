"""Migration proof for e6b2c8d4f0a1 (profile_section widened 20 → 50).

Prod incident 15/09/2026 18:03 UTC (Fly-Request-Id 01M2K3SCZH4GCRY2C6V2KM5PPK):
the agency section key `suivi_client_prospect` is 21 characters, allowed
by `agency_profile_section.key` String(50) since the sections lot, while
`custom_field_definition.profile_section` and
`company_field_definition.profile_section` stayed String(20) → asyncpg
« value too long for type character varying(20) » → 500 on every field
creation in that section. Roundtrip: widths before/after on BOTH tables,
the incident's key insertable after, refused before.
"""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "d5a1b7c3e9f2"
THIS = "e6b2c8d4f0a1"
TABLES = ("custom_field_definition", "company_field_definition")
INCIDENT_KEY = "suivi_client_prospect"  # 21 chars — the prod section


@pytest.fixture(scope="module")
def alembic_db():
    from src.core.config import get_settings

    saved = os.environ.get("DATABASE_URL_SYNC")
    with PostgresContainer("postgres:16-alpine") as pg:
        os.environ["DATABASE_URL_SYNC"] = pg.get_connection_url()
        get_settings.cache_clear()
        cfg = Config("alembic.ini")
        engine = create_engine(pg.get_connection_url())
        try:
            yield cfg, engine
        finally:
            engine.dispose()
            if saved is None:
                os.environ.pop("DATABASE_URL_SYNC", None)
            else:
                os.environ["DATABASE_URL_SYNC"] = saved
            get_settings.cache_clear()


def _width(engine, table: str) -> int:
    with engine.begin() as c:
        return c.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'profile_section'"
            ),
            {"t": table},
        ).scalar_one()


def _insert_definition(engine, section: str) -> None:
    """A definition on `section`, the way the manager writes one."""
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agency (id, name, slug, settings, created_at, updated_at) "
                "VALUES (:id, 'A', 'a', '{}'::jsonb, now(), now()) ON CONFLICT DO NOTHING"
            ),
            {"id": "00000000-0000-0000-0000-000000000001"},
        )
        c.execute(
            text(
                "INSERT INTO custom_field_definition "
                "(id, agency_id, key, label, label_i18n, field_type, scope, profile_section, "
                "required, position, created_at, updated_at) VALUES "
                "(gen_random_uuid(), '00000000-0000-0000-0000-000000000001', "
                ":key, 'Date dernier échange', '{}'::jsonb, 'text', 'person', :section, "
                "false, 0, now(), now())"
            ),
            {"key": f"k_{section[:10]}_{len(section)}", "section": section},
        )


def test_profile_section_width_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert {_width(engine, t) for t in TABLES} == {20}
    assert len(INCIDENT_KEY) == 21
    with pytest.raises(DBAPIError):  # the incident, reproduced at the column
        _insert_definition(engine, INCIDENT_KEY)

    command.upgrade(cfg, THIS)
    assert {_width(engine, t) for t in TABLES} == {50}
    _insert_definition(engine, INCIDENT_KEY)  # accepted now
    with engine.begin() as c:
        stored = c.execute(
            text("SELECT profile_section FROM custom_field_definition WHERE profile_section = :s"),
            {"s": INCIDENT_KEY},
        ).scalar_one()
    assert stored == INCIDENT_KEY

    # Downgrade refuses while a 21-char key is stored (the right outcome);
    # once the row is gone the shrink is clean, and the upgrade reapplies.
    with pytest.raises(Exception):  # noqa: B017 — alembic wraps the DBAPI error
        command.downgrade(cfg, PARENT)
    with engine.begin() as c:
        c.execute(text("DELETE FROM custom_field_definition"))
    command.downgrade(cfg, PARENT)
    assert {_width(engine, t) for t in TABLES} == {20}
    command.upgrade(cfg, THIS)
    assert {_width(engine, t) for t in TABLES} == {50}
