"""Migration proof for f4b8d0a6c2e8 (NID-07 hotfix).

The bug: notification_window + digest_cursor were created with NOT NULL
created_at/updated_at but NO server_default — while TimestampMixin
declares server_default=func.now(). The ORM omits created_at from the
INSERT, so prod (schema built by MIGRATIONS) raised NotNullViolation on
every insert → POST /cases 503. Tests missed it because the test harness
builds the schema from Base.metadata (create_all applies the model's
server_default); only the migration-built schema lacked it.

This test runs REAL Alembic and proves: at the PARENT revision, an
insert omitting created_at FAILS (reproduces prod); after THIS
migration, the same insert SUCCEEDS (the DB fills it)."""

import os
import uuid

import pytest
from alembic.config import Config
from psycopg2.errors import NotNullViolation
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "e0a6c4b8d2f6"
THIS = "f4b8d0a6c2e8"
_TABLES = ("notification_window", "digest_cursor")


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


def _default(engine, table: str, column: str) -> str | None:
    with engine.begin() as c:
        return c.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :col"
            ),
            {"t": table, "col": column},
        ).scalar()


def _seed_case_committed(engine) -> str:
    with engine.begin() as c:
        return _seed_case(c)


def _insert_window_omitting_created_at(engine, case_id: str) -> None:
    """The exact ORM shape: created_at/updated_at NOT in the column list.
    The case is seeded in a SEPARATE committed txn so this insert can only
    fail on the notification_window columns."""
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO notification_window (id, case_id, recipient_email, category, "
                "last_sent_at) VALUES (:id, :case_id, 'x@example.com', 'steps', now())"
            ),
            {"id": str(uuid.uuid4()), "case_id": case_id},
        )


def _seed_case(conn) -> str:
    """Minimal agency + expat + client_case to satisfy the FK chain."""
    agency_id, expat_id, case_id = (str(uuid.uuid4()) for _ in range(3))
    conn.execute(
        text(
            "INSERT INTO agency (id, name, slug, settings, created_at, updated_at) "
            "VALUES (:id, 'A', :slug, '{}'::jsonb, now(), now())"
        ),
        {"id": agency_id, "slug": f"a-{agency_id[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO expat_user (id, first_name, last_name, email, preferred_lang, "
            "created_at, updated_at) VALUES (:id, 'F', 'L', :email, 'fr', now(), now())"
        ),
        {"id": expat_id, "email": f"e-{expat_id[:8]}@example.com"},
    )
    conn.execute(
        text(
            "INSERT INTO client_case (id, agency_id, principal_expat_user_id, status, "
            "tags, created_at, updated_at) VALUES (:id, :agency, :expat, 'prospect', "
            "'[]'::jsonb, now(), now())"
        ),
        {"id": case_id, "agency": agency_id, "expat": expat_id},
    )
    return case_id


def test_migration_adds_missing_timestamp_default(alembic_db):
    cfg, engine = alembic_db

    # PARENT: reproduce the prod bug — no server_default, insert FAILS.
    command.upgrade(cfg, PARENT)
    for table in _TABLES:
        assert _default(engine, table, "created_at") is None  # the bug
    case_id = _seed_case_committed(engine)
    with pytest.raises(IntegrityError) as exc:
        _insert_window_omitting_created_at(engine, case_id)
    assert isinstance(exc.value.orig, NotNullViolation)  # the exact prod error
    assert "created_at" in str(exc.value.orig)  # on THE column, not a seed artefact

    # THIS: the fix — server_default present, the same insert SUCCEEDS.
    command.upgrade(cfg, THIS)
    for table in _TABLES:
        assert _default(engine, table, "created_at") is not None
        assert _default(engine, table, "updated_at") is not None
    _insert_window_omitting_created_at(engine, case_id)  # no raise → onboarding unblocked

    # Clean roundtrip.
    command.downgrade(cfg, PARENT)
    assert _default(engine, "notification_window", "created_at") is None
