"""Migration proof for f1a2b3c4d5e6 (crm_import_mapping: `name` into the key).

Run real Alembic against a DEDICATED testcontainer (the session harness builds
the schema with create_all, so the migration itself is exercised here):

  upgrade(parent) → insert a row with a NULL name (legal under the 3-col key)
  → upgrade(this) → the NULL is backfilled, `name` is NOT NULL, and the new
  4-col UNIQUE lets a second DIFFERENT name coexist while a same 4-col is
  rejected
  → down→up roundtrip on a single row → schema identical (name nullable again
  on the way down, NOT NULL back up).
"""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "d7f3a9c14e21"
THIS = "f1a2b3c4d5e6"


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


def _seed_parent_mapping(engine) -> dict[str, str]:
    """An agency + parcours + ONE mapping with a NULL name (legal at PARENT)."""
    ids = {k: str(uuid.uuid4()) for k in ("agency", "tpl", "m1")}
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agency (id, name, slug, settings, created_at, updated_at)"
                " VALUES (:id, 'A', 'a', '{}'::jsonb, now(), now())"
            ),
            {"id": ids["agency"]},
        )
        c.execute(
            text(
                "INSERT INTO journey_template (id, agency_id, name, created_at, updated_at)"
                " VALUES (:id, :ag, 'T', now(), now())"
            ),
            {"id": ids["tpl"], "ag": ids["agency"]},
        )
        c.execute(
            text(
                "INSERT INTO crm_import_mapping (id, agency_id, journey_template_id, crm_slug,"
                " name, mapping, created_at, updated_at)"
                " VALUES (:id, :ag, :tpl, 'hubspot-crm', NULL, '{}'::jsonb, now(), now())"
            ),
            {"id": ids["m1"], "ag": ids["agency"], "tpl": ids["tpl"]},
        )
    return ids


def _name_nullable(engine) -> bool:
    with engine.begin() as c:
        return (
            c.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns"
                    " WHERE table_name = 'crm_import_mapping' AND column_name = 'name'"
                )
            ).scalar()
            == "YES"
        )


def test_name_in_key_migration_backfill_and_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db

    # 1. PARENT — name is nullable; seed a NULL-name row.
    command.upgrade(cfg, PARENT)
    ids = _seed_parent_mapping(engine)
    assert _name_nullable(engine) is True

    # 2. Apply THIS — NULL backfilled to 'Import', column now NOT NULL.
    command.upgrade(cfg, THIS)
    assert _name_nullable(engine) is False
    with engine.begin() as c:
        name = c.execute(
            text("SELECT name FROM crm_import_mapping WHERE id = :id"),
            {"id": ids["m1"]},
        ).scalar()
    assert name == "Import"

    # 3. 4-col key: a DIFFERENT name for the same (agency, parcours, CRM) coexists…
    second = str(uuid.uuid4())
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO crm_import_mapping (id, agency_id, journey_template_id, crm_slug,"
                " name, mapping, created_at, updated_at)"
                " VALUES (:id, :ag, :tpl, 'hubspot-crm', 'test2', '{}'::jsonb, now(), now())"
            ),
            {"id": second, "ag": ids["agency"], "tpl": ids["tpl"]},
        )
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM crm_import_mapping")).scalar() == 2

    # …while a SAME 4-col (name 'test2' again) is rejected by the unique key.
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO crm_import_mapping (id, agency_id, journey_template_id, crm_slug,"
                " name, mapping, created_at, updated_at)"
                " VALUES (:id, :ag, :tpl, 'hubspot-crm', 'test2', '{}'::jsonb, now(), now())"
            ),
            {"id": str(uuid.uuid4()), "ag": ids["agency"], "tpl": ids["tpl"]},
        )

    # 4. Roundtrip: drop the extra row (the 3-col key allows one per CRM), then
    #    down→up — name nullable on the way down, NOT NULL back up (identical).
    with engine.begin() as c:
        c.execute(text("DELETE FROM crm_import_mapping WHERE id = :id"), {"id": second})
    command.downgrade(cfg, PARENT)
    assert _name_nullable(engine) is True
    command.upgrade(cfg, THIS)
    assert _name_nullable(engine) is False
