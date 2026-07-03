"""Migration proof for f6a2b8d4c0e1 (journey_template.editing_language).

Real Alembic against a DEDICATED testcontainer: parent → column absent;
upgrade → column present, NULL and supported values pass, an unsupported
value violates the CHECK; downgrade → column gone; re-upgrade → clean
roundtrip."""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "e5f1a7c3b9d2"
THIS = "f6a2b8d4c0e1"


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


def _column_exists(engine) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_name ="
                    " 'journey_template' AND column_name = 'editing_language'"
                )
            ).scalar()
        )


def _insert_template(engine, editing_language: str | None) -> str:
    tid = str(uuid.uuid4())
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO journey_template (id, name, name_i18n, is_sample,"
                " editing_language, created_at, updated_at)"
                " VALUES (:id, 'T', '{}'::jsonb, true, :lang, now(), now())"
            ),
            {"id": tid, "lang": editing_language},
        )
    return tid


def test_editing_language_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db

    command.upgrade(cfg, PARENT)
    assert not _column_exists(engine)

    command.upgrade(cfg, THIS)
    assert _column_exists(engine)
    _insert_template(engine, "es")  # supported value passes
    _insert_template(engine, None)  # NULL = no preference, passes
    with pytest.raises(IntegrityError):  # outside the referential: CHECK fires
        _insert_template(engine, "de")

    command.downgrade(cfg, PARENT)
    assert not _column_exists(engine)
    command.upgrade(cfg, THIS)
    assert _column_exists(engine)
