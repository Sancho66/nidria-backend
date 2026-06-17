"""Migration proof for b2e9d6a4c8f1 (step validator / "Action validée par").

This is the SENSITIVE migration (prod data). The normal harness builds the
schema via create_all, so the migration itself is exercised here against a
DEDICATED testcontainer, running real Alembic:

  upgrade(parent) → insert OLD rows (completion_mode, no validator columns)
  → upgrade(this) → assert the backfill covers EVERY row (no NULL left) with
  the exact mapping (auto→none, agency_validation→agent, agent_id NULL)
  → re-run the backfill (idempotent, no drift)
  → downgrade(parent) → validator columns gone, completion_mode INTACT
  (reversible, zero loss).
"""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "a1d4f8c2e6b9"
THIS = "b2e9d6a4c8f1"

# Backfill SQL mirrored from the migration — re-executed to prove the
# backfill is idempotent (re-runnable on prod without drift).
_BACKFILL_STEP = (
    "UPDATE journey_template_step SET default_validated_by_type ="
    " CASE completion_mode WHEN 'auto' THEN 'none'"
    " WHEN 'agency_validation' THEN 'agent' ELSE 'agent' END"
)
_BACKFILL_PROGRESS = (
    "UPDATE case_step_progress p SET validated_by_type ="
    " CASE (SELECT s.completion_mode FROM journey_template_step s"
    "       WHERE s.id = p.template_step_id)"
    " WHEN 'auto' THEN 'none' WHEN 'agency_validation' THEN 'agent' ELSE 'agent' END"
)


@pytest.fixture(scope="module")
def alembic_db():
    """Own container + Alembic config pointed at it (the session harness
    can't run migrations — it uses create_all). Restores the global settings
    cache + env on teardown so it never leaks into other modules."""
    from src.core.config import get_settings

    saved = os.environ.get("DATABASE_URL_SYNC")
    with PostgresContainer("postgres:16-alpine") as pg:
        sync_url = pg.get_connection_url()  # postgresql+psycopg2://...
        os.environ["DATABASE_URL_SYNC"] = sync_url
        get_settings.cache_clear()  # env.py reads settings.database_url_sync
        cfg = Config("alembic.ini")
        engine = create_engine(sync_url)
        try:
            yield cfg, engine
        finally:
            engine.dispose()
            if saved is None:
                os.environ.pop("DATABASE_URL_SYNC", None)
            else:
                os.environ["DATABASE_URL_SYNC"] = saved
            get_settings.cache_clear()


def _seed_old_rows(engine) -> dict[str, str]:
    """Insert a minimal valid graph AT THE PARENT revision (no validator
    columns): an agency, an expat, a case, a template with two steps
    (auto + agency_validation) and their two progress instances."""
    ids = {
        k: str(uuid.uuid4())
        for k in ("agency", "expat", "case", "tpl", "s_auto", "s_val", "p_auto", "p_val")
    }
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
                "INSERT INTO expat_user (id, first_name, last_name, email, preferred_lang,"
                " created_at, updated_at)"
                " VALUES (:id, 'M', 'C', 'm@x.com', 'fr', now(), now())"
            ),
            {"id": ids["expat"]},
        )
        c.execute(
            text(
                "INSERT INTO client_case (id, agency_id, principal_expat_user_id, status, tags,"
                " created_at, updated_at)"
                " VALUES (:id, :ag, :ex, 'prospect', '[]'::jsonb, now(), now())"
            ),
            {"id": ids["case"], "ag": ids["agency"], "ex": ids["expat"]},
        )
        c.execute(
            text(
                "INSERT INTO journey_template (id, agency_id, name, created_at, updated_at)"
                " VALUES (:id, :ag, 'T', now(), now())"
            ),
            {"id": ids["tpl"], "ag": ids["agency"]},
        )
        for key, pos, mode in (("s_auto", 0, "auto"), ("s_val", 1, "agency_validation")):
            c.execute(
                text(
                    "INSERT INTO journey_template_step (id, template_id, name, position,"
                    " completion_mode, created_at, updated_at)"
                    " VALUES (:id, :tpl, :nm, :pos, :mode, now(), now())"
                ),
                {"id": ids[key], "tpl": ids["tpl"], "nm": key, "pos": pos, "mode": mode},
            )
        for pkey, skey in (("p_auto", "s_auto"), ("p_val", "s_val")):
            c.execute(
                text(
                    "INSERT INTO case_step_progress (id, case_id, template_step_id, status,"
                    " created_at, updated_at)"
                    " VALUES (:id, :case, :step, 'todo', now(), now())"
                ),
                {"id": ids[pkey], "case": ids["case"], "step": ids[skey]},
            )
    return ids


def test_migration_backfill_idempotent_and_reversible(alembic_db) -> None:
    cfg, engine = alembic_db

    # 1. Schema at the PARENT (no validator columns yet).
    command.upgrade(cfg, PARENT)
    ids = _seed_old_rows(engine)

    # 2. Apply the validator migration → backfill.
    command.upgrade(cfg, THIS)

    with engine.begin() as c:
        # EVERY template step backfilled with the exact mapping — no NULL.
        rows = dict(
            c.execute(
                text("SELECT completion_mode, default_validated_by_type FROM journey_template_step")
            ).fetchall()
        )
        assert rows == {"auto": "none", "agency_validation": "agent"}
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM journey_template_step"
                    " WHERE default_validated_by_type IS NULL"
                )
            ).scalar()
            == 0
        )
        # EVERY progress instance backfilled (covers all rows, no orphan).
        prog = dict(
            c.execute(text("SELECT id::text, validated_by_type FROM case_step_progress")).fetchall()
        )
        assert prog[ids["p_auto"]] == "none"
        assert prog[ids["p_val"]] == "agent"
        assert (
            c.execute(
                text("SELECT count(*) FROM case_step_progress WHERE validated_by_type IS NULL")
            ).scalar()
            == 0
        )
        # agent_id left NULL (no historical designation) — CHECK still holds.
        designated = c.execute(
            text("SELECT count(*) FROM case_step_progress WHERE validated_by_agent_id IS NOT NULL")
        ).scalar()
        assert designated == 0

    # 3. Idempotency: re-running the backfill changes nothing, raises nothing.
    with engine.begin() as c:
        c.execute(text(_BACKFILL_STEP))
        c.execute(text(_BACKFILL_PROGRESS))
        again = dict(
            c.execute(
                text("SELECT completion_mode, default_validated_by_type FROM journey_template_step")
            ).fetchall()
        )
        assert again == {"auto": "none", "agency_validation": "agent"}

    # 4. Reversibility: downgrade drops the validator columns and leaves
    #    completion_mode untouched (zero loss → rollback restores 100%).
    command.downgrade(cfg, PARENT)
    with engine.begin() as c:
        cols = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'case_step_progress'"
                )
            ).fetchall()
        }
        assert "validated_by_type" not in cols
        assert "validated_by_agent_id" not in cols
        tpl_cols = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'journey_template_step'"
                )
            ).fetchall()
        }
        assert "default_validated_by_type" not in tpl_cols
        # completion_mode survived intact — the rollback-safe source of truth.
        modes = {
            r[0]
            for r in c.execute(text("SELECT completion_mode FROM journey_template_step")).fetchall()
        }
        assert modes == {"auto", "agency_validation"}
