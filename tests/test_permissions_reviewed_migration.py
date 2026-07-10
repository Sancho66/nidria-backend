"""Migration proof for c4d9e6a2b8f5 (role.permissions_reviewed_at): additive,
reversible, idempotent — and the backfill is asserted on a real pre-migration
row (permissions_reviewed_at = created_at, NOT the migration's now())."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "b3c8d5f1a2e4"
THIS = "c4d9e6a2b8f5"

RID = "11111111-1111-1111-1111-111111111111"
PAST = "2020-01-01 00:00:00+00"


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
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'role' AND column_name = 'permissions_reviewed_at'"
                )
            ).scalar()
        )


def _assert_backfilled(engine) -> None:
    with engine.begin() as c:
        row = c.execute(
            text(
                "SELECT created_at, permissions_reviewed_at, "
                "permissions_reviewed_at = created_at AS equal,"
                " count(*) OVER () FROM role WHERE id = :id"
            ),
            {"id": RID},
        ).one()
        # THE backfill semantics: the last known decision of a pre-existing
        # role is its CREATION — not the migration's own now().
        assert row.equal is True, (row.created_at, row.permissions_reviewed_at)
        nulls = c.execute(
            text("SELECT count(*) FROM role WHERE permissions_reviewed_at IS NULL")
        ).scalar()
        assert nulls == 0


def test_permissions_reviewed_at_backfill_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not _has_column(engine)
    # A pre-migration role frozen in the past (a clone-like row: system role
    # with an old created_at is enough — the backfill rule is role-wide).
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO role (id, agency_id, name, is_system, created_at, updated_at)"
                " VALUES (:id, NULL, 'frozen', true, :past, now())"
            ),
            {"id": RID, "past": PAST},
        )

    command.upgrade(cfg, THIS)
    assert _has_column(engine)
    _assert_backfilled(engine)

    command.downgrade(cfg, PARENT)  # reversible
    assert not _has_column(engine)

    command.upgrade(cfg, THIS)  # idempotent re-apply, same backfill
    _assert_backfilled(engine)
