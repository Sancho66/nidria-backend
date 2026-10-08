"""Migration proof for j7d3f9b5e1c4 (job.manage becomes platform-only), on a
dedicated testcontainer, real alembic run, rows seeded in raw SQL at the
parent revision:

- a database without the job.manage row (fresh: the catalogue is synced at
  boot, after the migrations) passes both directions untouched;
- upgrade: no role but the system superadmin keeps job.manage — the system
  admin, its copy-on-write clones, a clone of superadmin and a plain custom
  role all lose it; every other grant stays;
- downgrade: the system admin and its clones get it back (even the clone
  whose agency had dropped it: documented), the custom role and the clone of
  superadmin do not (documented: not restored);
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

PARENT = "h9e5b1f7c3d0"
THIS = "j7d3f9b5e1c4"

AGENCY = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_AGENCY = "aaaaaaaa-0000-0000-0000-000000000002"
JOB_MANAGE = "bbbbbbbb-0000-0000-0000-000000000001"
CASE_VIEW = "bbbbbbbb-0000-0000-0000-000000000002"
ADMIN = "cccccccc-0000-0000-0000-000000000001"
SUPERADMIN = "cccccccc-0000-0000-0000-000000000002"
ADMIN_CLONE = "cccccccc-0000-0000-0000-000000000003"
# An admin clone whose agency had removed job.manage on purpose.
ADMIN_CLONE_WITHOUT = "cccccccc-0000-0000-0000-000000000004"
SUPERADMIN_CLONE = "cccccccc-0000-0000-0000-000000000005"
CUSTOM = "cccccccc-0000-0000-0000-000000000006"
ALL_ROLES = (ADMIN, SUPERADMIN, ADMIN_CLONE, ADMIN_CLONE_WITHOUT, SUPERADMIN_CLONE, CUSTOM)


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
        "job_manage_platform_only_migration",
        Path(__file__).parents[1] / f"alembic/versions/{THIS}_job_manage_platform_only.py",
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
    """The state before the decision: job.manage on the system admin, its
    clone, superadmin, a clone of superadmin and a custom role; case.view
    on every role (the grant that must survive both directions)."""
    with engine.begin() as c:
        for agency_id, slug in ((AGENCY, "jm-agency"), (OTHER_AGENCY, "jm-other")):
            c.execute(
                text(
                    "INSERT INTO agency (id, name, slug, settings)"
                    " VALUES (:id, :slug, :slug, '{}'::jsonb)"
                ),
                {"id": agency_id, "slug": slug},
            )
        for permission_id, key in ((JOB_MANAGE, "job.manage"), (CASE_VIEW, "case.view")):
            c.execute(
                text(
                    "INSERT INTO permission (id, key, label, category)"
                    " VALUES (:id, :key, :key, split_part(:key, '.', 1))"
                ),
                {"id": permission_id, "key": key},
            )
        roles = (
            (ADMIN, None, "admin", True, None),
            (SUPERADMIN, None, "superadmin", True, None),
            (ADMIN_CLONE, AGENCY, "admin", False, ADMIN),
            (ADMIN_CLONE_WITHOUT, OTHER_AGENCY, "admin", False, ADMIN),
            (SUPERADMIN_CLONE, AGENCY, "Opérateurs", False, SUPERADMIN),
            (CUSTOM, AGENCY, "ops", False, None),
        )
        for role_id, agency_id, name, is_system, cloned_from in roles:
            c.execute(
                text(
                    "INSERT INTO role (id, agency_id, name, is_system, cloned_from_role_id)"
                    " VALUES (:id, :agency, :name, :system, :origin)"
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
            (role_id, JOB_MANAGE)
            for role_id in (ADMIN, SUPERADMIN, ADMIN_CLONE, SUPERADMIN_CLONE, CUSTOM)
        ]
        for role_id, permission_id in grants:
            c.execute(
                text("INSERT INTO role_permission (role_id, permission_id) VALUES (:r, :p)"),
                {"r": role_id, "p": permission_id},
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


def test_job_manage_leaves_every_role_but_superadmin_and_comes_back_to_admin(
    alembic_db,
) -> None:
    cfg, engine = alembic_db

    # A fresh database: no permission row yet, both directions are no-ops.
    command.upgrade(cfg, THIS)
    command.downgrade(cfg, PARENT)
    assert _grant_count(engine) == 0

    _seed(engine)
    assert _holders(engine, JOB_MANAGE) == {
        ADMIN,
        SUPERADMIN,
        ADMIN_CLONE,
        SUPERADMIN_CLONE,
        CUSTOM,
    }

    command.upgrade(cfg, THIS)
    assert _holders(engine, JOB_MANAGE) == {SUPERADMIN}
    assert _holders(engine, CASE_VIEW) == set(ALL_ROLES)  # only job.manage moved
    _apply_again(engine, "upgrade")
    assert _holders(engine, JOB_MANAGE) == {SUPERADMIN}

    command.downgrade(cfg, PARENT)
    # The admin default comes back, ADMIN_CLONE_WITHOUT included; the custom
    # role and the clone of superadmin are not restored.
    assert _holders(engine, JOB_MANAGE) == {
        SUPERADMIN,
        ADMIN,
        ADMIN_CLONE,
        ADMIN_CLONE_WITHOUT,
    }
    assert _holders(engine, CASE_VIEW) == set(ALL_ROLES)
    _apply_again(engine, "downgrade")
    assert _holders(engine, JOB_MANAGE) == {
        SUPERADMIN,
        ADMIN,
        ADMIN_CLONE,
        ADMIN_CLONE_WITHOUT,
    }

    command.upgrade(cfg, THIS)  # the round trip lands on the same state
    assert _holders(engine, JOB_MANAGE) == {SUPERADMIN}
    assert _holders(engine, CASE_VIEW) == set(ALL_ROLES)
