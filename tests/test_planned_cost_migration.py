"""Migration proof for a2f7c4e9b1d3 (planned costs): creates journey_step_cost,
makes case_step_cost.amount nullable, adds planned_amount +
source_template_cost_id. Reversible + idempotent on a dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "f5c2a8d1e9b3"
THIS = "a2f7c4e9b1d3"


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


def _has_table(engine, table: str) -> bool:
    with engine.begin() as c:
        return bool(c.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar())


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


def _amount_nullable(engine) -> bool:
    with engine.begin() as c:
        return (
            c.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns"
                    " WHERE table_name = 'case_step_cost' AND column_name = 'amount'"
                )
            ).scalar()
            == "YES"
        )


def _assert_upgraded(engine) -> None:
    assert _has_table(engine, "journey_step_cost")
    assert _has_column(engine, "case_step_cost", "planned_amount")
    assert _has_column(engine, "case_step_cost", "source_template_cost_id")
    assert _amount_nullable(engine)


def _assert_downgraded(engine) -> None:
    assert not _has_table(engine, "journey_step_cost")
    assert not _has_column(engine, "case_step_cost", "planned_amount")
    assert not _has_column(engine, "case_step_cost", "source_template_cost_id")
    assert not _amount_nullable(engine)  # amount is NOT NULL again


def test_planned_costs_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    _assert_downgraded(engine)  # the pre-migration state
    command.upgrade(cfg, THIS)
    _assert_upgraded(engine)
    command.downgrade(cfg, PARENT)
    _assert_downgraded(engine)  # reversible
    command.upgrade(cfg, THIS)
    _assert_upgraded(engine)  # idempotent re-apply
