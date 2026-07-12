"""Migration proof for e6f1a8c4d2b7 (Paddle billing): the 4 agency columns
(billing_mode defaulting EXISTING rows to 'manual' — the non-migration) +
paddle_webhook_event. Additive, reversible, idempotent on a testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "d5e0f7b3c9a6"
THIS = "e6f1a8c4d2b7"

AID = "11111111-1111-1111-1111-111111111111"


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


def _upgraded(engine) -> bool:
    with engine.begin() as c:
        cols = c.execute(
            text(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'agency' AND column_name IN"
                " ('billing_mode', 'billing_status', 'paddle_customer_id',"
                " 'paddle_subscription_id')"
            )
        ).scalar()
        table = c.execute(text("SELECT to_regclass('paddle_webhook_event')")).scalar()
        return cols == 4 and table is not None


def test_paddle_billing_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _upgraded(engine)
    # A pre-existing agency: the upgrade must default it to 'manual'.
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agency (id, name, slug, settings)"
                " VALUES (:id, 'A', 'a-slug', '{}'::jsonb)"
            ),
            {"id": AID},
        )

    command.upgrade(cfg, THIS)
    assert _upgraded(engine)
    with engine.begin() as c:
        row = c.execute(
            text("SELECT billing_mode, billing_status FROM agency WHERE id = :id"), {"id": AID}
        ).one()
        # The non-migration of existing agencies IS the default.
        assert row == ("manual", None)

    command.downgrade(cfg, PARENT)  # reversible
    assert not _upgraded(engine)
    command.upgrade(cfg, THIS)  # idempotent re-apply
    assert _upgraded(engine)
