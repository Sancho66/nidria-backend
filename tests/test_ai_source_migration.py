"""Migration proof for d9f6a2c4e8b1 (ai_translation_source): additive,
RLS enabled (post-sweep rule), clean roundtrip on a dedicated
testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "c8e5f1b3d7a0"
THIS = "d9f6a2c4e8b1"
TABLE = "ai_translation_source"


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


def test_ai_translation_source_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _exists(engine, TABLE)
    command.upgrade(cfg, THIS)
    assert _exists(engine, TABLE)
    assert _rls(engine, TABLE)
    command.downgrade(cfg, PARENT)
    assert not _exists(engine, TABLE)
    command.upgrade(cfg, THIS)
    assert _exists(engine, TABLE)
