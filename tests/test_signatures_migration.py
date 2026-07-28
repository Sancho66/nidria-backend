"""Migration proof du méga-lot signatures (c4f0a2e8d6b0 + d6a2c8e4f0b2) :
additif pur (4 colonnes défaut serveur + 4 tables), roundtrip propre sur un
testcontainer dédié — une exigence existante reçoit signature_required=false
/ level 'ses' sans backfill."""

import os

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "b2d8f4a6c0e2"
LOT1 = "c4f0a2e8d6b0"
LOT2 = "d6a2c8e4f0b2"
LOT6 = "e8b4d0f6a2c4"


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


def _has_table(engine, table: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
                {"t": table},
            ).scalar()
        )


def _has_column(engine, table: str, column: str) -> bool:
    with engine.begin() as c:
        return bool(
            c.execute(
                text(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar()
        )


def test_signatures_roundtrip(alembic_db) -> None:
    cfg, engine = alembic_db

    command.upgrade(cfg, PARENT)
    assert not _has_column(engine, "step_requirement", "signature_required")
    assert not _has_table(engine, "signature_request")

    command.upgrade(cfg, LOT6)
    for table in ("step_requirement", "case_step_requirement"):
        assert _has_column(engine, table, "signature_required")
        assert _has_column(engine, table, "signature_level")
        # LOT 6 : la source du document (chemin + nom), additive nullable.
        assert _has_column(engine, table, "signature_document_path")
        assert _has_column(engine, table, "signature_document_filename")
    for table in (
        "signature_request",
        "signature_signer",
        "signature_credit_balance",
        "signature_credit_entry",
    ):
        assert _has_table(engine, table)

    # Les défauts serveur tiennent : une ligne préexistante (insérée sans
    # les colonnes) lit false / 'ses'.
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO journey_template (id, name, name_i18n, is_sample, origin)"
                " VALUES ('11111111-1111-1111-1111-111111111111', 'T', '{}', false, 'user')"
            )
        )
        c.execute(
            text(
                "INSERT INTO journey_template_step (id, template_id, name, position)"
                " VALUES ('22222222-2222-2222-2222-222222222222',"
                " '11111111-1111-1111-1111-111111111111', 'S', 1)"
            )
        )
        c.execute(
            text(
                "INSERT INTO step_requirement (id, step_id, kind, reference, scope, position)"
                " VALUES ('33333333-3333-3333-3333-333333333333',"
                " '22222222-2222-2222-2222-222222222222', 'document', 'Doc', 'principal', 0)"
            )
        )
        row = c.execute(
            text(
                "SELECT signature_required, signature_level FROM step_requirement"
                " WHERE id = '33333333-3333-3333-3333-333333333333'"
            )
        ).one()
        assert (row.signature_required, row.signature_level) == (False, "ses")
        # L'invariant DB du ledger : un solde négatif est IMPOSSIBLE.
        c.execute(
            text(
                "INSERT INTO agency (id, name, slug, settings)"
                " VALUES ('44444444-4444-4444-4444-444444444444', 'A', 'a-sig-mig', '{}')"
            )
        )
        import sqlalchemy.exc

        try:
            c.execute(
                text(
                    "INSERT INTO signature_credit_balance (id, agency_id, available, reserved)"
                    " VALUES ('55555555-5555-5555-5555-555555555555',"
                    " '44444444-4444-4444-4444-444444444444', -1, 0)"
                )
            )
            raise AssertionError("negative balance must be rejected")
        except sqlalchemy.exc.IntegrityError:
            pass

    command.downgrade(cfg, PARENT)
    assert not _has_column(engine, "step_requirement", "signature_required")
    assert not _has_column(engine, "step_requirement", "signature_document_path")
    assert not _has_table(engine, "signature_credit_entry")
    command.upgrade(cfg, LOT6)
    assert _has_table(engine, "signature_signer")
    assert _has_column(engine, "case_step_requirement", "signature_document_path")
