"""Migration proof for f0c2e8a4b6d1 (agency subscription fields):
additive columns + founding CHECK, clean roundtrip on a dedicated
testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "e1b7c3a9d5f2"
THIS = "f0c2e8a4b6d1"
COLUMNS = (
    "plan",
    "billing_cycle",
    "seats_included",
    "founding_free_seats",
    "base_price_eur",
    "seat_price_eur",
    "price_locked_until",
    "is_founding",
    "converted_at",
)


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


def _present(engine) -> set[str]:
    with engine.begin() as c:
        rows = c.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'agency'")
        ).scalars()
        return {r for r in rows if r in COLUMNS}


def _check_exists(engine) -> bool:
    # The metadata naming convention prefixes check names (ck_agency_...):
    # match on the stable fragment, not the exact templated name.
    with engine.begin() as c:
        return bool(
            c.execute(
                text("SELECT 1 FROM pg_constraint WHERE conname LIKE '%founding_free_seats%'")
            ).scalar()
        )


def test_subscription_fields_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert _present(engine) == set()
    command.upgrade(cfg, THIS)
    assert _present(engine) == set(COLUMNS)
    assert _check_exists(engine)
    command.downgrade(cfg, PARENT)
    assert _present(engine) == set()
    command.upgrade(cfg, THIS)
    assert _present(engine) == set(COLUMNS)
