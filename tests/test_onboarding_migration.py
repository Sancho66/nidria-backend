"""Migration proof for e1b7c3a9d5f2 (agency.onboarding_dismissed_at):
additive column, clean roundtrip on a dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "d9f6a2c4e8b1"
THIS = "e1b7c3a9d5f2"


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


def _column_exists(engine) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'agency' AND column_name = 'onboarding_dismissed_at'"
                )
            ).scalar()
        )


def test_onboarding_dismissed_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _column_exists(engine)
    command.upgrade(cfg, THIS)
    assert _column_exists(engine)
    command.downgrade(cfg, PARENT)
    assert not _column_exists(engine)
    command.upgrade(cfg, THIS)
    assert _column_exists(engine)
