"""Migration proof for v7e3c9d5a2b8 (agency.signup_alert_sent_at): additive
nullable column, clean roundtrip on a dedicated testcontainer.

The column is deliberately separate from onboarding_email_sent_at — this test
also pins that the two coexist, so a future edit cannot quietly merge them."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "u6d2b8e4f1a3"
THIS = "v7e3c9d5a2b8"


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


def _has_column(engine, name: str = "signup_alert_sent_at") -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_name = 'agency' AND column_name = :n"
                ),
                {"n": name},
            ).scalar()
        )


def test_signup_alert_flag_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _has_column(engine)
    command.upgrade(cfg, THIS)
    assert _has_column(engine)
    # The onboarding flag survives untouched: two mails, two flags.
    assert _has_column(engine, "onboarding_email_sent_at")
    command.downgrade(cfg, PARENT)
    assert not _has_column(engine)
    assert _has_column(engine, "onboarding_email_sent_at")
    command.upgrade(cfg, THIS)
    assert _has_column(engine)
