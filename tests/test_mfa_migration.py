"""Migration proof for b9d5f1a3c7e2 (mfa_totp + mfa_backup_code +
mfa_challenge): additive, RLS enabled (post-sweep rule), clean roundtrip
on a dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "a8c4e2f6b0d3"
THIS = "b9d5f1a3c7e2"
TABLES = ("mfa_totp", "mfa_backup_code", "mfa_challenge")


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


def test_mfa_tables_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db

    command.upgrade(cfg, PARENT)
    assert not any(_exists(engine, t) for t in TABLES)

    command.upgrade(cfg, THIS)
    assert all(_exists(engine, t) for t in TABLES)
    assert all(_rls(engine, t) for t in TABLES)

    command.downgrade(cfg, PARENT)
    assert not any(_exists(engine, t) for t in TABLES)
    command.upgrade(cfg, THIS)
    assert all(_exists(engine, t) for t in TABLES)
