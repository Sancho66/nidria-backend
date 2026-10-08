"""The Email module's migration (h9e5b1f7c3d0): reversible, deny-all RLS on
its nine tables, and a schema IDENTICAL to the one the models produce.

The test database is born from the models (`create_all`, trigger included
through `after_create`); dev and production are born from this migration. The
round trip snapshots the model-born schema, downgrades, upgrades, and compares
everything a divergence could hide in: columns (type, length, nullability,
server default), every constraint (name and definition, ON DELETE included),
every index (name and definition, partial WHERE included), the freeze trigger
and its function, row-level security. Lesson D15: what differs between model
and migration is invisible to the suite and alive in production.
"""

import importlib.util
from pathlib import Path
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, inspect, text

from shared.models.base import Base

TABLES = (
    "agency_email_module",
    "email_campaign",
    "email_campaign_step",
    "email_campaign_ai_variable",
    "email_campaign_member",
    "email_member_render",
    "email_provider_event",
    "email_message",
    "email_suppression",
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "email_campaigns_migration",
        Path(__file__).parents[1] / "alembic/versions/h9e5b1f7c3d0_email_campaigns.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(connection: Connection) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for table in TABLES:
        columns = connection.execute(
            text(
                "SELECT column_name, data_type, character_maximum_length, is_nullable,"
                " column_default FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).all()
        constraints = connection.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = CAST(:t AS regclass)"
            ),
            {"t": table},
        ).all()
        indexes = connection.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
            {"t": table},
        ).all()
        triggers = connection.execute(
            text(
                "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger"
                " WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal"
            ),
            {"t": table},
        ).all()
        rls = connection.scalar(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
        )
        snap[table] = {
            "columns": {row[0]: tuple(row[1:]) for row in columns},
            "constraints": dict(constraints),
            "indexes": dict(indexes),
            "triggers": dict(triggers),
            "rls": rls,
        }
    source = connection.scalar(
        text("SELECT prosrc FROM pg_proc WHERE proname = 'email_render_freeze'")
    )
    snap["function"] = " ".join(source.split()) if source else None
    return snap


def test_email_campaigns_migration_round_trip(sync_engine: Engine) -> None:
    migration = _migration()
    assert migration.down_revision == "f7c3d9e5a1b2"
    with sync_engine.begin() as connection:
        born_from_models = _snapshot(connection)
        assert born_from_models["function"] is not None
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.downgrade()
            assert not set(TABLES) & set(inspect(connection).get_table_names())
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_proc WHERE proname = 'email_render_freeze'")
                )
                == 0
            )
            migration.upgrade()
        born_from_migration = _snapshot(connection)
    for table in TABLES:
        assert born_from_migration[table] == born_from_models[table], table
        assert born_from_migration[table]["rls"] is True, f"{table} without RLS"
    assert born_from_migration["function"] == born_from_models["function"]
    assert born_from_migration["email_member_render"]["triggers"].keys() == {"email_render_freeze"}


def test_every_model_default_is_also_a_database_default() -> None:
    """A Python default without a server default would let a raw INSERT (a
    job, a script) fail where the ORM succeeds. Only the client-side UUID
    primary key is exempt, as everywhere in Nidria."""
    missing = [
        f"{table}.{column.name}"
        for table in TABLES
        for column in Base.metadata.tables[table].columns
        if column.default is not None and column.server_default is None and column.name != "id"
    ]
    assert missing == []
