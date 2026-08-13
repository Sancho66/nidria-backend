"""Migration proof for u6d2b8e4f1a3 (agency.onboarding_email_sent_at):
additive, nullable, no backfill (the pre-feature park keeps a NULL flag and
stays out of the sweep's window), clean roundtrip on a dedicated
testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "t4f8c2a6d9e1"
THIS = "u6d2b8e4f1a3"
COLUMN = "onboarding_email_sent_at"


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


def _column(engine) -> tuple[str, str] | None:
    with engine.begin() as c:
        return c.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'agency' AND column_name = :col"
            ),
            {"col": COLUMN},
        ).first()


def test_onboarding_flag_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db

    command.upgrade(cfg, PARENT)
    assert _column(engine) is None

    command.upgrade(cfg, THIS)
    row = _column(engine)
    assert row is not None
    # Nullable and WITHOUT default: an existing agency stays NULL, and the
    # flag can only be posed by a real send.
    assert row[0] == "YES" and row[1] is None

    command.downgrade(cfg, PARENT)
    assert _column(engine) is None
    command.upgrade(cfg, THIS)
    assert _column(engine) is not None
