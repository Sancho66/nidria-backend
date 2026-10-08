"""Migration proof for k3f8b2d6a9e1 (platform permissions leave every role but
the system superadmin), on a dedicated testcontainer, real alembic run, rows
seeded in raw SQL at the parent revision:

- a fresh database (no permission row: the catalogue is synced at boot,
  after the migrations) passes both directions untouched;
- upgrade: agency.create, platform.task_manage and job.manage stay on the
  system superadmin only. The clones of superadmin (what the escalation of
  08/10/2026 minted), an admin clone, a plain custom role, an agency role
  named "superadmin", and the system roles of the agencies (admin, member, an
  external one) all lose them; every other grant stays; no agent moves, no
  clone's last matrix decision moves;
- downgrade gives nothing back;
- each direction applied twice: no error, same state.
"""

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

pytestmark = pytest.mark.migration

PARENT = "j7d3f9b5e1c4"
THIS = "k3f8b2d6a9e1"

AGENCY = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_AGENCY = "aaaaaaaa-0000-0000-0000-000000000002"
AGENCY_CREATE = "bbbbbbbb-0000-0000-0000-000000000001"
TASK_MANAGE = "bbbbbbbb-0000-0000-0000-000000000002"
JOB_MANAGE = "bbbbbbbb-0000-0000-0000-000000000003"
CASE_VIEW = "bbbbbbbb-0000-0000-0000-000000000004"
PLATFORM = (AGENCY_CREATE, TASK_MANAGE, JOB_MANAGE)
SUPERADMIN = "cccccccc-0000-0000-0000-000000000001"
ADMIN = "cccccccc-0000-0000-0000-000000000002"
MEMBER = "cccccccc-0000-0000-0000-000000000003"
EXTERNAL = "cccccccc-0000-0000-0000-000000000004"
SUPERADMIN_CLONE = "cccccccc-0000-0000-0000-000000000005"
SUPERADMIN_CLONE_OTHER = "cccccccc-0000-0000-0000-000000000006"
ADMIN_CLONE = "cccccccc-0000-0000-0000-000000000007"
CUSTOM = "cccccccc-0000-0000-0000-000000000008"
HOMONYM = "cccccccc-0000-0000-0000-000000000009"
ALL_ROLES = (
    SUPERADMIN,
    ADMIN,
    MEMBER,
    EXTERNAL,
    SUPERADMIN_CLONE,
    SUPERADMIN_CLONE_OTHER,
    ADMIN_CLONE,
    CUSTOM,
    HOMONYM,
)
# Who held each platform permission before the cleanup.
HELD_BEFORE = {
    AGENCY_CREATE: {SUPERADMIN, ADMIN, SUPERADMIN_CLONE, SUPERADMIN_CLONE_OTHER, HOMONYM},
    TASK_MANAGE: {SUPERADMIN, MEMBER, SUPERADMIN_CLONE, SUPERADMIN_CLONE_OTHER, CUSTOM, HOMONYM},
    JOB_MANAGE: {
        SUPERADMIN,
        EXTERNAL,
        SUPERADMIN_CLONE,
        SUPERADMIN_CLONE_OTHER,
        ADMIN_CLONE,
        CUSTOM,
        HOMONYM,
    },
}
OPERATOR = "dddddddd-0000-0000-0000-000000000001"
INVITEE = "dddddddd-0000-0000-0000-000000000002"


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


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "platform_permissions_superadmin_only_migration",
        Path(__file__).parents[1]
        / f"alembic/versions/{THIS}_platform_permissions_superadmin_only.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply_again(engine: Engine, direction: str) -> None:
    """Run one direction's body once more on a database it already ran on."""
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            getattr(_migration(), direction)()


def _seed(engine: Engine) -> None:
    """The state the hole may have left, plus earlier slips: platform
    permissions on clones of superadmin, on other agency rows and on the
    agencies' system roles; case.view on every role (the grant that must
    survive); an operator on the system superadmin, an invitee on a clone."""
    with engine.begin() as c:
        for agency_id, slug in ((AGENCY, "pp-agency"), (OTHER_AGENCY, "pp-other")):
            c.execute(
                text(
                    "INSERT INTO agency (id, name, slug, settings)"
                    " VALUES (:id, :slug, :slug, '{}'::jsonb)"
                ),
                {"id": agency_id, "slug": slug},
            )
        for permission_id, key in (
            (AGENCY_CREATE, "agency.create"),
            (TASK_MANAGE, "platform.task_manage"),
            (JOB_MANAGE, "job.manage"),
            (CASE_VIEW, "case.view"),
        ):
            c.execute(
                text(
                    "INSERT INTO permission (id, key, label, category)"
                    " VALUES (:id, :key, :key, split_part(:key, '.', 1))"
                ),
                {"id": permission_id, "key": key},
            )
        roles = (
            (SUPERADMIN, None, "superadmin", True, False, None),
            (ADMIN, None, "admin", True, False, None),
            (MEMBER, None, "member", True, False, None),
            (EXTERNAL, None, "external_lawyer", True, True, None),
            (SUPERADMIN_CLONE, AGENCY, "Equipe", False, False, SUPERADMIN),
            (SUPERADMIN_CLONE_OTHER, OTHER_AGENCY, "Opérateurs", False, False, SUPERADMIN),
            (ADMIN_CLONE, AGENCY, "admin", False, False, ADMIN),
            (CUSTOM, AGENCY, "ops", False, False, None),
            (HOMONYM, OTHER_AGENCY, "superadmin", False, False, None),
        )
        for role_id, agency_id, name, is_system, is_external, cloned_from in roles:
            c.execute(
                text(
                    "INSERT INTO role (id, agency_id, name, is_system, is_external,"
                    " cloned_from_role_id, permissions_reviewed_at)"
                    " VALUES (:id, :agency, :name, :system, :external, :origin,"
                    " '2026-06-12T00:00:00Z')"
                ),
                {
                    "id": role_id,
                    "agency": agency_id,
                    "name": name,
                    "system": is_system,
                    "external": is_external,
                    "origin": cloned_from,
                },
            )
        grants = [(role_id, CASE_VIEW) for role_id in ALL_ROLES] + [
            (role_id, permission_id)
            for permission_id, holders in HELD_BEFORE.items()
            for role_id in holders
        ]
        for role_id, permission_id in grants:
            c.execute(
                text("INSERT INTO role_permission (role_id, permission_id) VALUES (:r, :p)"),
                {"r": role_id, "p": permission_id},
            )
        for agent_id, role_id, email in (
            (OPERATOR, SUPERADMIN, "operator@example.com"),
            (INVITEE, SUPERADMIN_CLONE, "invitee@example.com"),
        ):
            c.execute(
                text(
                    "INSERT INTO agent (id, agency_id, role_id, email, first_name,"
                    " last_name, password_hash)"
                    " VALUES (:id, :agency, :role, :email, 'Test', 'Agent', 'x')"
                ),
                {"id": agent_id, "agency": AGENCY, "role": role_id, "email": email},
            )


def _holders(engine: Engine, permission_id: str) -> set[str]:
    with engine.begin() as c:
        rows = c.execute(
            text("SELECT role_id FROM role_permission WHERE permission_id = :p"),
            {"p": permission_id},
        ).scalars()
        return {str(role_id) for role_id in rows}


def _grant_count(engine: Engine) -> int:
    with engine.begin() as c:
        return int(c.execute(text("SELECT count(*) FROM role_permission")).scalar_one())


def _untouched_rows(engine: Engine) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(agent, role) pairs and (role, last matrix decision) pairs."""
    with engine.begin() as c:
        agents = {
            (str(agent_id), str(role_id))
            for agent_id, role_id in c.execute(text("SELECT id, role_id FROM agent"))
        }
        decisions = {
            (str(role_id), reviewed.isoformat())
            for role_id, reviewed in c.execute(text("SELECT id, permissions_reviewed_at FROM role"))
        }
    return agents, decisions


def _assert_cleaned(engine: Engine, untouched: object) -> None:
    for permission_id in PLATFORM:
        assert _holders(engine, permission_id) == {SUPERADMIN}, permission_id
    assert _holders(engine, CASE_VIEW) == set(ALL_ROLES)  # only platform rows moved
    assert _untouched_rows(engine) == untouched


def test_platform_permissions_stay_on_the_system_superadmin_only(alembic_db) -> None:
    cfg, engine = alembic_db

    # A fresh database: no permission row yet, both directions are no-ops.
    command.upgrade(cfg, THIS)
    command.downgrade(cfg, PARENT)
    assert _grant_count(engine) == 0

    _seed(engine)
    for permission_id, holders in HELD_BEFORE.items():
        assert _holders(engine, permission_id) == holders
    untouched = _untouched_rows(engine)

    command.upgrade(cfg, THIS)
    _assert_cleaned(engine, untouched)
    _apply_again(engine, "upgrade")
    _assert_cleaned(engine, untouched)

    # Nothing given back, whichever way it runs.
    command.downgrade(cfg, PARENT)
    _assert_cleaned(engine, untouched)
    _apply_again(engine, "downgrade")
    _assert_cleaned(engine, untouched)

    command.upgrade(cfg, THIS)  # the round trip lands on the same state
    _assert_cleaned(engine, untouched)
