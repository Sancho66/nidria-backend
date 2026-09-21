"""The additive monitor table has deny-all RLS and a reversible migration."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text


def test_email_account_state_migration(sync_engine):
    spec = importlib.util.spec_from_file_location(
        "email_monitor_migration",
        Path(__file__).parents[1] / "alembic/versions/f7c3d9e5a1b2_add_email_account_state.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with sync_engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.downgrade()
            assert "email_account_state" not in inspect(connection).get_table_names()
            migration.upgrade()
            columns = {c["name"] for c in inspect(connection).get_columns("email_account_state")}
            assert columns == {"provider", "state", "created_at", "updated_at"}
            assert connection.scalar(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = 'email_account_state'")
            )
