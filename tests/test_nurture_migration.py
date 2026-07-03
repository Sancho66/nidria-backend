"""Migration proof for e4c1a7d3b9f5 (nurture_send): additive, RLS
enabled (post-sweep rule), clean roundtrip on a dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "d2a8c4f0e6b1"
THIS = "e4c1a7d3b9f5"
TABLE = "nurture_send"


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


def _exists(engine, table: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(text("SELECT to_regclass(:q) IS NOT NULL"), {"q": f"public.{table}"}).scalar()
        )


def _rls(engine, table: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
            ).scalar()
        )


def _unique_slot(engine) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_nurture_send_slot'")
            ).scalar()
        )


def test_nurture_send_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db

    command.upgrade(cfg, PARENT)
    assert not _exists(engine, TABLE)

    command.upgrade(cfg, THIS)
    assert _exists(engine, TABLE)
    assert _rls(engine, TABLE)
    assert _unique_slot(engine)

    command.downgrade(cfg, PARENT)
    assert not _exists(engine, TABLE)
    command.upgrade(cfg, THIS)
    assert _exists(engine, TABLE)
