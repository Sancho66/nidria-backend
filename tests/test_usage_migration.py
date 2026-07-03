"""Migration proof for d2a8c4f0e6b1 (usage_event + agency_usage_milestone
+ client_case.is_demo + agency.trial_ends_at): additive, RLS enabled on
the two new tables (post-sweep rule), clean roundtrip on a dedicated
testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "c1f7b3e9d5a4"
THIS = "d2a8c4f0e6b1"
TABLES = ("usage_event", "agency_usage_milestone")


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


def _column(engine, table: str, column: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar()
        )


def test_usage_trackers_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db

    command.upgrade(cfg, PARENT)
    assert not any(_exists(engine, t) for t in TABLES)
    assert not _column(engine, "client_case", "is_demo")
    assert not _column(engine, "agency", "trial_ends_at")

    command.upgrade(cfg, THIS)
    assert all(_exists(engine, t) for t in TABLES)
    assert all(_rls(engine, t) for t in TABLES)
    assert _column(engine, "client_case", "is_demo")
    assert _column(engine, "agency", "trial_ends_at")

    command.downgrade(cfg, PARENT)
    assert not any(_exists(engine, t) for t in TABLES)
    assert not _column(engine, "client_case", "is_demo")
    assert not _column(engine, "agency", "trial_ends_at")
    command.upgrade(cfg, THIS)
    assert all(_exists(engine, t) for t in TABLES)
