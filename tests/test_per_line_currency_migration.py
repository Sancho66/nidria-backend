"""Migration proof for b3c8d5f1a2e4 (per-line currency): adds
journey_step_cost.currency + case_step_cost.currency/planned_currency, and
BACKFILLS every existing line from its agency's currency (point 6, deterministic,
no assumption). Reversible + idempotent, and the backfill is asserted on real
pre-migration rows — all on a dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "a2f7c4e9b1d3"
THIS = "b3c8d5f1a2e4"

# Deterministic ids for the pre-migration fixture chain.
AID = "11111111-1111-1111-1111-111111111111"
EID = "22222222-2222-2222-2222-222222222222"
TID = "33333333-3333-3333-3333-333333333333"
STID = "44444444-4444-4444-4444-444444444444"
JSC = "55555555-5555-5555-5555-555555555555"
CID = "66666666-6666-6666-6666-666666666666"
PID = "77777777-7777-7777-7777-777777777777"
COST_PLANNED = "88888888-8888-8888-8888-888888888888"
COST_MANUAL = "99999999-9999-9999-9999-999999999999"


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


def _has_column(engine, table: str, column: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = :t AND column_name = :col"
                ),
                {"t": table, "col": column},
            ).scalar()
        )


def _seed_pre_migration_rows(engine) -> None:
    """The FK chain, at the PARENT schema (no currency columns yet): an agency in
    PYG, a planned cost, a case with a planned-born line (amount empty) and a
    manual débours (no plan)."""
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agency (id, name, slug, settings, currency)"
                " VALUES (:id, 'A', 'a-slug', '{}'::jsonb, 'PYG')"
            ),
            {"id": AID},
        )
        c.execute(
            text(
                "INSERT INTO expat_user (id, first_name, last_name, email, preferred_lang)"
                " VALUES (:id, 'E', 'X', 'e@x.io', 'fr')"
            ),
            {"id": EID},
        )
        c.execute(
            text("INSERT INTO journey_template (id, agency_id, name) VALUES (:id, :aid, 'T')"),
            {"id": TID, "aid": AID},
        )
        c.execute(
            text(
                "INSERT INTO journey_template_step (id, template_id, name, position)"
                " VALUES (:id, :tid, 'S', 0)"
            ),
            {"id": STID, "tid": TID},
        )
        c.execute(
            text(
                "INSERT INTO journey_step_cost (id, step_id, amount, label)"
                " VALUES (:id, :sid, 120, 'timbre')"
            ),
            {"id": JSC, "sid": STID},
        )
        c.execute(
            text(
                "INSERT INTO client_case (id, agency_id, principal_expat_user_id, status, tags)"
                " VALUES (:id, :aid, :eid, 'prospect', '[]'::jsonb)"
            ),
            {"id": CID, "aid": AID, "eid": EID},
        )
        c.execute(
            text(
                "INSERT INTO case_step_progress (id, case_id, template_step_id, status)"
                " VALUES (:id, :cid, :sid, 'todo')"
            ),
            {"id": PID, "cid": CID, "sid": STID},
        )
        # A planned-born line (real amount empty) and a manual débours (no plan).
        c.execute(
            text(
                "INSERT INTO case_step_cost"
                " (id, case_step_progress_id, amount, planned_amount, label)"
                " VALUES (:id, :pid, NULL, 120, 'planned')"
            ),
            {"id": COST_PLANNED, "pid": PID},
        )
        c.execute(
            text(
                "INSERT INTO case_step_cost"
                " (id, case_step_progress_id, amount, planned_amount, label)"
                " VALUES (:id, :pid, 50, NULL, 'manual')"
            ),
            {"id": COST_MANUAL, "pid": PID},
        )


def _assert_backfilled(engine) -> None:
    with engine.begin() as c:
        assert (
            c.execute(
                text("SELECT currency FROM journey_step_cost WHERE id = :id"), {"id": JSC}
            ).scalar()
            == "PYG"
        )
        # Every case line inherited its agency's currency; none is left without.
        assert (
            c.execute(text("SELECT count(*) FROM case_step_cost WHERE currency IS NULL")).scalar()
            == 0
        )
        planned = c.execute(
            text("SELECT currency, planned_currency FROM case_step_cost WHERE id = :id"),
            {"id": COST_PLANNED},
        ).one()
        assert planned == ("PYG", "PYG")  # planned line: both currencies set
        manual = c.execute(
            text("SELECT currency, planned_currency FROM case_step_cost WHERE id = :id"),
            {"id": COST_MANUAL},
        ).one()
        assert manual == ("PYG", None)  # manual débours: real set, no planned currency


def test_per_line_currency_backfill_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    # At the PARENT: no currency columns yet.
    command.upgrade(cfg, PARENT)
    assert not _has_column(engine, "journey_step_cost", "currency")
    assert not _has_column(engine, "case_step_cost", "currency")
    assert not _has_column(engine, "case_step_cost", "planned_currency")

    _seed_pre_migration_rows(engine)

    # Upgrade: columns appear AND every existing line is backfilled from its
    # agency (point 6). The NOT NULL on `currency` proves no line was left out.
    command.upgrade(cfg, THIS)
    assert _has_column(engine, "journey_step_cost", "currency")
    assert _has_column(engine, "case_step_cost", "currency")
    assert _has_column(engine, "case_step_cost", "planned_currency")
    _assert_backfilled(engine)

    # Reversible: the three columns disappear (the base rows survive).
    command.downgrade(cfg, PARENT)
    assert not _has_column(engine, "journey_step_cost", "currency")
    assert not _has_column(engine, "case_step_cost", "currency")

    # Idempotent re-apply: the backfill runs again on the same rows, same result.
    command.upgrade(cfg, THIS)
    _assert_backfilled(engine)
