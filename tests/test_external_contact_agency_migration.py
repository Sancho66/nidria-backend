"""Migration proof for f2c9a1d5e7b3 (external_contact agency scope):
additive, backfill-safe, and a clean up→down→up roundtrip (idempotence +
reversibility) on a dedicated testcontainer."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "a1d3f5b7c9e2"
THIS = "f2c9a1d5e7b3"


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


def _col(engine, table: str, col: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name=:t AND column_name=:c"
                ),
                {"t": table, "c": col},
            ).scalar()
        )


def _nullable(engine, table: str, col: str) -> bool:
    with engine.begin() as c:
        return (
            c.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name=:t AND column_name=:c"
                ),
                {"t": table, "c": col},
            ).scalar()
            == "YES"
        )


def _index(engine, name: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text("SELECT count(*) FROM pg_indexes WHERE indexname=:n"), {"n": name}
            ).scalar()
        )


def _constraint_def(engine, conname: str) -> str | None:
    with engine.begin() as c:
        return c.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname=:n"),
            {"n": conname},
        ).scalar()


def _check_allows_agent(engine) -> bool:
    definition = _constraint_def(engine, "ck_reminder_recipient_type_matches_fk")
    return definition is not None and "'agent'" in definition


def _participant_allows_external(engine) -> bool:
    # The constraint name is truncated by Postgres (63 chars) with a hash
    # suffix, so match by TABLE, not by the full name.
    with engine.begin() as c:
        defs = (
            c.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='journey_step_participant'::regclass AND contype='c'"
                )
            )
            .scalars()
            .all()
        )
    return any("'external'" in d for d in defs)


def _assert_upgraded(engine) -> None:
    assert _col(engine, "external_contact", "agency_id")
    assert not _nullable(engine, "external_contact", "agency_id")
    assert _nullable(engine, "external_contact", "case_id")  # now optional
    assert _col(engine, "external_contact", "agent_id")
    assert _index(engine, "uq_external_contact_directory_name")
    assert _check_allows_agent(engine)
    assert _col(engine, "journey_step_participant", "external_id")
    assert _participant_allows_external(engine)


def _assert_downgraded(engine) -> None:
    assert not _col(engine, "external_contact", "agency_id")
    assert not _col(engine, "external_contact", "agent_id")
    assert not _nullable(engine, "external_contact", "case_id")  # NOT NULL restored
    assert not _index(engine, "uq_external_contact_directory_name")
    assert not _check_allows_agent(engine)
    assert not _col(engine, "journey_step_participant", "external_id")
    assert not _participant_allows_external(engine)


def test_external_contact_agency_scope_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    _assert_downgraded(engine)  # baseline: none of the new schema yet
    command.upgrade(cfg, THIS)
    _assert_upgraded(engine)
    command.downgrade(cfg, PARENT)
    _assert_downgraded(engine)  # reversibility
    command.upgrade(cfg, THIS)
    _assert_upgraded(engine)  # idempotent re-apply
