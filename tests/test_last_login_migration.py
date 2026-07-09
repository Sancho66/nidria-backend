"""Migration proof for f5c2a8d1e9b3 (agent.last_login_at): additive,
reversible, idempotent roundtrip on a dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "e4b1a2c6d8f0"
THIS = "f5c2a8d1e9b3"


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


def _has_column(engine) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'agent' AND column_name = 'last_login_at'"
                )
            ).scalar()
        )


def test_last_login_at_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _has_column(engine)
    command.upgrade(cfg, THIS)
    assert _has_column(engine)
    command.downgrade(cfg, PARENT)
    assert not _has_column(engine)
    command.upgrade(cfg, THIS)
    assert _has_column(engine)
