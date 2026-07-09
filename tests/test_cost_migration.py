"""Migration proof for the cost feature: d3a9c1e5b7f2 (agency.currency) then
e4b1a2c6d8f0 (case_step_cost). Both additive, reversible, idempotent roundtrip
on a dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "b4f8d2a6c1e9"
CURRENCY = "d3a9c1e5b7f2"
COST = "e4b1a2c6d8f0"


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


def _has_currency(engine) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'agency' AND column_name = 'currency'"
                )
            ).scalar()
        )


def _has_cost_table(engine) -> bool:
    with engine.begin() as c:
        return bool(c.execute(text("SELECT to_regclass('public.case_step_cost')")).scalar())


def test_cost_migrations_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _has_currency(engine) and not _has_cost_table(engine)

    command.upgrade(cfg, CURRENCY)
    assert _has_currency(engine) and not _has_cost_table(engine)

    command.upgrade(cfg, COST)
    assert _has_currency(engine) and _has_cost_table(engine)

    # Reversible, step by step.
    command.downgrade(cfg, CURRENCY)
    assert _has_currency(engine) and not _has_cost_table(engine)
    command.downgrade(cfg, PARENT)
    assert not _has_currency(engine) and not _has_cost_table(engine)

    # Idempotent re-apply to head.
    command.upgrade(cfg, COST)
    assert _has_currency(engine) and _has_cost_table(engine)
