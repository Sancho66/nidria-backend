"""Migration proof for a6f2c8e4d0b9 (journey_section.seed_key): additive
nullable column + per-template unique, clean roundtrip on a dedicated
testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "f5d2b8e4c1a6"
THIS = "a6f2c8e4d0b9"


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
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_name = 'journey_section' AND column_name = 'seed_key'"
                )
            ).scalar()
        )


def _has_unique(engine) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_section_seed_key'")
            ).scalar()
        )


def test_seed_key_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _has_column(engine)
    command.upgrade(cfg, THIS)
    assert _has_column(engine)
    assert _has_unique(engine)
    command.downgrade(cfg, PARENT)
    assert not _has_column(engine)
    command.upgrade(cfg, THIS)
    assert _has_column(engine)
