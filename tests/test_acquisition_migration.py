"""Migration proof for w8f4d1c7e3a9 (acquisition + contact_phone): purely
additive nullable columns on TWO tables, clean roundtrip.

Aucun backfill n'est possible ni souhaitable : la source d'une inscription
est périssable, les agences déjà créées n'en ont pas et n'en auront jamais.
Ce test grave donc aussi que le parc existant reste à NULL — une valeur
inventée serait pire qu'un NULL honnête."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "v7e3c9d5a2b8"
THIS = "w8f4d1c7e3a9"

ACQUISITION = ("utm_source", "utm_medium", "utm_campaign", "referrer", "acquisition_captured_at")


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


def _cols(engine, table: str) -> set[str]:
    with engine.begin() as c:
        return {
            r[0]
            for r in c.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table},
            )
        }


def test_acquisition_and_phone_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)
    assert not (_cols(engine, "agency") & set(ACQUISITION))
    assert "contact_phone" not in _cols(engine, "agency")
    assert not (_cols(engine, "signup_verification") & set(ACQUISITION))

    command.upgrade(cfg, THIS)
    assert set(ACQUISITION) <= _cols(engine, "agency")
    assert "contact_phone" in _cols(engine, "agency")
    assert set(ACQUISITION) <= _cols(engine, "signup_verification")
    # Le voisin de palier est intact : contact_phone s'ajoute À CÔTÉ.
    assert "contact_email" in _cols(engine, "agency")

    command.downgrade(cfg, PARENT)
    assert not (_cols(engine, "agency") & set(ACQUISITION))
    assert "contact_phone" not in _cols(engine, "agency")
    assert "contact_email" in _cols(engine, "agency")

    command.upgrade(cfg, THIS)
    assert set(ACQUISITION) <= _cols(engine, "agency")


def test_the_existing_park_stays_null(alembic_db) -> None:
    """Une agence créée AVANT le lot n'a pas de source, et n'en aura pas."""
    cfg, engine = alembic_db
    command.upgrade(cfg, THIS)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agency (id, name, slug, settings, created_at, updated_at)"
                " VALUES (gen_random_uuid(), 'Ancienne', 'ancienne-park', '{}'::jsonb,"
                " now(), now())"
            )
        )
        row = c.execute(
            text(
                "SELECT utm_source, acquisition_captured_at, contact_phone"
                " FROM agency WHERE slug = 'ancienne-park'"
            )
        ).one()
    assert row == (None, None, None)
