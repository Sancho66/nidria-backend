"""Apply the complete migration chain in an isolated PostgreSQL database."""

import os

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from testcontainers.postgres import PostgresContainer

from alembic import command
from src.core.config import get_settings


def test_support_and_one_off_migration_roundtrip() -> None:
    saved = os.environ.get("DATABASE_URL_SYNC")
    with PostgresContainer("postgres:16-alpine") as pg:
        os.environ["DATABASE_URL_SYNC"] = pg.get_connection_url()
        get_settings.cache_clear()
        cfg = Config("alembic.ini")
        engine = create_engine(pg.get_connection_url())
        try:
            command.upgrade(cfg, "m7e3b9d5f1a8")
            before = {c["name"] for c in inspect(engine).get_columns("document_template")}
            assert "case_id" not in before
            command.upgrade(cfg, "head")
            with engine.begin() as conn:
                for table in ("support_access_grant", "support_access_event"):
                    assert (
                        conn.scalar(
                            text("SELECT relrowsecurity FROM pg_class WHERE relname=:name"),
                            {"name": table},
                        )
                        is True
                    )
                    assert (
                        conn.scalar(
                            text("SELECT count(*) FROM pg_policies WHERE tablename=:name"),
                            {"name": table},
                        )
                        == 0
                    )
                assert (
                    conn.scalar(text("SELECT version_num FROM alembic_version")) == "p9a5d1f7b3c0"
                )
            columns = {c["name"] for c in inspect(engine).get_columns("document_template")}
            assert columns == before | {
                "case_id",
                "case_step_progress_id",
                "person_id",
                "submitted_at",
            }
            command.downgrade(cfg, "m7e3b9d5f1a8")
            assert "support_access_grant" not in inspect(engine).get_table_names()
            assert {c["name"] for c in inspect(engine).get_columns("document_template")} == before
            command.upgrade(cfg, "head")
        finally:
            engine.dispose()
            if saved is None:
                os.environ.pop("DATABASE_URL_SYNC", None)
            else:
                os.environ["DATABASE_URL_SYNC"] = saved
            get_settings.cache_clear()
