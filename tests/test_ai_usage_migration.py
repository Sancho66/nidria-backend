"""Migration proof for b7d4e0a2c6f8 (agency_ai_usage): additive, RLS
enabled (post-sweep rule), unique (agency, month), clean roundtrip on a
dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "a6f2c8e4d0b9"
THIS = "b7d4e0a2c6f8"
TABLE = "agency_ai_usage"


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


def _unique(engine) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT count(*) FROM pg_constraint WHERE conname = 'uq_agency_ai_usage_month'"
                )
            ).scalar()
        )


def test_ai_usage_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _exists(engine, TABLE)
    command.upgrade(cfg, THIS)
    assert _exists(engine, TABLE)
    assert _rls(engine, TABLE)
    assert _unique(engine)
    command.downgrade(cfg, PARENT)
    assert not _exists(engine, TABLE)
    command.upgrade(cfg, THIS)
    assert _exists(engine, TABLE)
