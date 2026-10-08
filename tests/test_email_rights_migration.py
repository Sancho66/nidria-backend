"""Migration proof for l5c9e2a7d4f1 (the Email keys leave every role but the
system superadmin: the platform grants them), on a dedicated testcontainer,
real alembic run, rows seeded in raw SQL at the parent revision:

- a fresh database (no permission row: the catalogue is synced at boot,
  after the migrations) passes both directions untouched;
- upgrade: the six `email.*` keys stay on the system superadmin only. The
  system admin, its clones, a clone of case_manager and a custom role an
  agency had granted them to (the old rule), a clone of superadmin, and an
  agency role named "superadmin" all lose them; every other grant stays; no
  agent moves, no role's last matrix decision moves;
- downgrade: the system admin gets them back (the old default), nobody else
  (documented: the clones' and custom roles' grants are not restored);
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

PARENT = "k3f8b2d6a9e1"
THIS = "l5c9e2a7d4f1"

AGENCY = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_AGENCY = "aaaaaaaa-0000-0000-0000-000000000002"
EMAIL_KEYS = (
    "email.view",
    "email.manage",
    "email.enroll",
    "email.pause",
    "email.remove",
    "email.sync",
)
EMAIL = tuple(f"bbbbbbbb-0000-0000-0000-00000000000{i}" for i in range(1, 7))
CASE_VIEW = "bbbbbbbb-0000-0000-0000-000000000007"
SUPERADMIN = "cccccccc-0000-0000-0000-000000000001"
ADMIN = "cccccccc-0000-0000-0000-000000000002"
CASE_MANAGER = "cccccccc-0000-0000-0000-000000000003"
ADMIN_CLONE = "cccccccc-0000-0000-0000-000000000004"
ADMIN_CLONE_OTHER = "cccccccc-0000-0000-0000-000000000005"
CASE_MANAGER_CLONE = "cccccccc-0000-0000-0000-000000000006"
CUSTOM = "cccccccc-0000-0000-0000-000000000007"
SUPERADMIN_CLONE = "cccccccc-0000-0000-0000-000000000008"
HOMONYM = "cccccccc-0000-0000-0000-000000000009"
ALL_ROLES = (
    SUPERADMIN,
    ADMIN,
    CASE_MANAGER,
    ADMIN_CLONE,
    ADMIN_CLONE_OTHER,
    CASE_MANAGER_CLONE,
    CUSTOM,
    SUPERADMIN_CLONE,
    HOMONYM,
)
# Who held the six keys before the decision: the old default (superadmin,
# admin, its clones) and the old rule's agency grants. CASE_MANAGER never.
HELD_BEFORE = {
    SUPERADMIN,
    ADMIN,
    ADMIN_CLONE,
    ADMIN_CLONE_OTHER,
    CASE_MANAGER_CLONE,
    CUSTOM,
    SUPERADMIN_CLONE,
    HOMONYM,
}
ADMIN_AGENT = "dddddddd-0000-0000-0000-000000000001"
OPERATOR = "dddddddd-0000-0000-0000-000000000002"


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
        "email_permissions_platform_granted_migration",
        Path(__file__).parents[1]
        / f"alembic/versions/{THIS}_email_permissions_platform_granted.py",
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
    """The state before the decision: the six keys on the old default and on
    the old rule's agency grants; case.view on every role (the grant that
    must survive); an admin on the admin clone, an operator on superadmin."""
    with engine.begin() as c:
        for agency_id, slug in ((AGENCY, "er-agency"), (OTHER_AGENCY, "er-other")):
            c.execute(
                text(
                    "INSERT INTO agency (id, name, slug, settings)"
                    " VALUES (:id, :slug, :slug, '{}'::jsonb)"
                ),
                {"id": agency_id, "slug": slug},
            )
        for permission_id, key in (*zip(EMAIL, EMAIL_KEYS, strict=True), (CASE_VIEW, "case.view")):
            c.execute(
                text(
                    "INSERT INTO permission (id, key, label, category)"
                    " VALUES (:id, :key, :key, split_part(:key, '.', 1))"
                ),
                {"id": permission_id, "key": key},
            )
        roles = (
            (SUPERADMIN, None, "superadmin", True, None),
            (ADMIN, None, "admin", True, None),
            (CASE_MANAGER, None, "case_manager", True, None),
            (ADMIN_CLONE, AGENCY, "admin", False, ADMIN),
            (ADMIN_CLONE_OTHER, OTHER_AGENCY, "Direction", False, ADMIN),
            (CASE_MANAGER_CLONE, AGENCY, "case_manager", False, CASE_MANAGER),
            (CUSTOM, AGENCY, "Prospection", False, None),
            (SUPERADMIN_CLONE, OTHER_AGENCY, "Opérateurs", False, SUPERADMIN),
            (HOMONYM, AGENCY, "superadmin", False, None),
        )
        for role_id, agency_id, name, is_system, cloned_from in roles:
            c.execute(
                text(
                    "INSERT INTO role (id, agency_id, name, is_system, cloned_from_role_id,"
                    " permissions_reviewed_at)"
                    " VALUES (:id, :agency, :name, :system, :origin, '2026-10-07T00:00:00Z')"
                ),
                {
                    "id": role_id,
                    "agency": agency_id,
                    "name": name,
                    "system": is_system,
                    "origin": cloned_from,
                },
            )
        grants = [(role_id, CASE_VIEW) for role_id in ALL_ROLES] + [
            (role_id, permission_id) for role_id in HELD_BEFORE for permission_id in EMAIL
        ]
        for role_id, permission_id in grants:
            c.execute(
                text("INSERT INTO role_permission (role_id, permission_id) VALUES (:r, :p)"),
                {"r": role_id, "p": permission_id},
            )
        for agent_id, role_id, email in (
            (ADMIN_AGENT, ADMIN_CLONE, "admin@example.com"),
            (OPERATOR, SUPERADMIN, "operator@example.com"),
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


def _assert_held_by(engine: Engine, holders: set[str], untouched: object) -> None:
    for permission_id in EMAIL:
        assert _holders(engine, permission_id) == holders, permission_id
    assert _holders(engine, CASE_VIEW) == set(ALL_ROLES)  # only email.* rows moved
    assert _untouched_rows(engine) == untouched


def test_the_email_keys_stay_on_the_system_superadmin_only(alembic_db) -> None:
    cfg, engine = alembic_db

    # A fresh database: no permission row yet, both directions are no-ops.
    command.upgrade(cfg, THIS)
    command.downgrade(cfg, PARENT)
    assert _grant_count(engine) == 0

    _seed(engine)
    for permission_id in EMAIL:
        assert _holders(engine, permission_id) == HELD_BEFORE
    untouched = _untouched_rows(engine)

    command.upgrade(cfg, THIS)
    _assert_held_by(engine, {SUPERADMIN}, untouched)
    _apply_again(engine, "upgrade")
    _assert_held_by(engine, {SUPERADMIN}, untouched)

    # The old default comes back on the system admin only: the clones' and
    # the custom role's grants are not restored.
    command.downgrade(cfg, PARENT)
    _assert_held_by(engine, {SUPERADMIN, ADMIN}, untouched)
    _apply_again(engine, "downgrade")
    _assert_held_by(engine, {SUPERADMIN, ADMIN}, untouched)

    command.upgrade(cfg, THIS)  # the round trip lands on the same state
    _assert_held_by(engine, {SUPERADMIN}, untouched)
