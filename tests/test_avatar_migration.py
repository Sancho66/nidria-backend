"""Migration proof for a8c4e2f6b0d3 (avatar_path on agent + expat_user):
additive, reversible, clean roundtrip on a dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "f6a2b8d4c0e1"
THIS = "a8c4e2f6b0d3"


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


def _has_column(engine, table: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = :t AND column_name = 'avatar_path'"
                ),
                {"t": table},
            ).scalar()
        )


def test_avatar_path_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db

    command.upgrade(cfg, PARENT)
    assert not _has_column(engine, "agent") and not _has_column(engine, "expat_user")

    command.upgrade(cfg, THIS)
    assert _has_column(engine, "agent") and _has_column(engine, "expat_user")

    command.downgrade(cfg, PARENT)
    assert not _has_column(engine, "agent") and not _has_column(engine, "expat_user")
    command.upgrade(cfg, THIS)
    assert _has_column(engine, "agent") and _has_column(engine, "expat_user")
