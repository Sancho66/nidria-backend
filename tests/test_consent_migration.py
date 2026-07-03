"""Migration proof for d4e8f2a6c1b7 (consent_document + consent_acceptance).

Real Alembic against a DEDICATED testcontainer (the session harness builds
the schema with create_all, so the migration itself is exercised here):

  upgrade(parent) → upgrade(this) → both tables exist with RLS enabled,
  UNIQUE(type, version) holds, and the acceptance key is NULLS NOT
  DISTINCT (a duplicate with a NULL agency_id is rejected too)
  → downgrade(parent) → tables gone → upgrade(this) again (clean
  roundtrip, re-runnable)."""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "a103249eb0a1"
THIS = "d4e8f2a6c1b7"


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


def _table_exists(engine, name: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text("SELECT to_regclass(:qualified) IS NOT NULL"),
                {"qualified": f"public.{name}"},
            ).scalar()
        )


def _rls_enabled(engine, name: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = :name"),
                {"name": name},
            ).scalar()
        )


def _insert_document(engine, doc_type: str, version: int) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO consent_document (id, type, version, content_md, content_hash,"
                " published_at, is_active, created_at, updated_at)"
                " VALUES (:id, :type, :version, 'x', 'h', now(), true, now(), now())"
            ),
            {"id": str(uuid.uuid4()), "type": doc_type, "version": version},
        )


def _insert_acceptance(engine, actor_id: str, agency_id: str | None) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO consent_acceptance (id, actor_type, actor_id, document_type,"
                " document_version, content_hash, accepted_at, ip, agency_id)"
                " VALUES (:id, 'agent', :actor, 'agency_terms', 1, 'h', now(), NULL, :agency)"
            ),
            {"id": str(uuid.uuid4()), "actor": actor_id, "agency": agency_id},
        )


def test_consent_tables_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db

    # 1. PARENT: neither table exists yet.
    command.upgrade(cfg, PARENT)
    assert not _table_exists(engine, "consent_document")
    assert not _table_exists(engine, "consent_acceptance")

    # 2. THIS: both tables, RLS enabled (post-sweep rule for new tables).
    command.upgrade(cfg, THIS)
    assert _table_exists(engine, "consent_document")
    assert _table_exists(engine, "consent_acceptance")
    assert _rls_enabled(engine, "consent_document")
    assert _rls_enabled(engine, "consent_acceptance")

    # 3. UNIQUE(type, version): a duplicate version is rejected.
    _insert_document(engine, "agency_terms", 1)
    with pytest.raises(IntegrityError):
        _insert_document(engine, "agency_terms", 1)
    _insert_document(engine, "agency_terms", 2)  # a new version is fine

    # 4. Acceptance key is NULLS NOT DISTINCT: the same acceptance with a
    # NULL agency_id cannot be recorded twice either.
    actor = str(uuid.uuid4())
    _insert_acceptance(engine, actor, agency_id=None)
    with pytest.raises(IntegrityError):
        _insert_acceptance(engine, actor, agency_id=None)

    # 5. Roundtrip: down to PARENT (tables gone), up again (re-runnable).
    command.downgrade(cfg, PARENT)
    assert not _table_exists(engine, "consent_document")
    assert not _table_exists(engine, "consent_acceptance")
    command.upgrade(cfg, THIS)
    assert _table_exists(engine, "consent_acceptance")
