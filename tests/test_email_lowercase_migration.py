"""Migration proof for e5f1a7c3b9d2 (lowercase identity emails).

Real Alembic against a DEDICATED testcontainer:

  upgrade(parent) → seed an agent with a MiXeD-case email → upgrade(this)
  → the email is lowercased in place
  → collision guard: two rows differing only by case make the migration
  ABORT loudly (merging accounts is a human decision), and the failed
  transaction leaves the version at parent → cleanup → re-run passes
  → downgrade is a clean data no-op."""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "d4e8f2a6c1b7"
THIS = "e5f1a7c3b9d2"


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


def _seed_agent(engine, email: str) -> str:
    """An agency + role + one agent with the given email casing."""
    ids = {k: str(uuid.uuid4()) for k in ("agency", "role", "agent")}
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agency (id, name, slug, settings, created_at, updated_at)"
                " VALUES (:id, 'A', :slug, '{}'::jsonb, now(), now())"
            ),
            {"id": ids["agency"], "slug": f"a-{ids['agency'][:8]}"},
        )
        c.execute(
            text(
                "INSERT INTO role (id, agency_id, name, is_system, created_at, updated_at)"
                " VALUES (:id, :ag, :name, false, now(), now())"
            ),
            {"id": ids["role"], "ag": ids["agency"], "name": f"r-{ids['role'][:8]}"},
        )
        c.execute(
            text(
                "INSERT INTO agent (id, agency_id, role_id, first_name, last_name, email,"
                " password_hash, is_external, created_at, updated_at)"
                " VALUES (:id, :ag, :role, 'A', 'B', :email, 'x', false, now(), now())"
            ),
            {"id": ids["agent"], "ag": ids["agency"], "role": ids["role"], "email": email},
        )
    return ids["agent"]


def _agent_email(engine, agent_id: str) -> str:
    with engine.begin() as c:
        return c.execute(
            text("SELECT email FROM agent WHERE id = :id"), {"id": agent_id}
        ).scalar_one()


def _seed_case_invitations(engine, emails: list[str]) -> None:
    """A case + one invitation per email — invitations have NO email
    uniqueness (one per case, re-sends), the exact shape that must NOT
    trip the collision guard (local db-upgrade regression)."""
    ids = {k: str(uuid.uuid4()) for k in ("agency", "expat", "case")}
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO agency (id, name, slug, settings, created_at, updated_at)"
                " VALUES (:id, 'A', :slug, '{}'::jsonb, now(), now())"
            ),
            {"id": ids["agency"], "slug": f"a-{ids['agency'][:8]}"},
        )
        c.execute(
            text(
                "INSERT INTO expat_user (id, first_name, last_name, email, preferred_lang,"
                " created_at, updated_at)"
                " VALUES (:id, 'C', 'D', :email, 'fr', now(), now())"
            ),
            {"id": ids["expat"], "email": f"principal-{ids['expat'][:8]}@example.com"},
        )
        c.execute(
            text(
                "INSERT INTO client_case (id, agency_id, principal_expat_user_id, status,"
                " tags, created_at, updated_at)"
                " VALUES (:id, :ag, :expat, 'prospect', '[]'::jsonb, now(), now())"
            ),
            {"id": ids["case"], "ag": ids["agency"], "expat": ids["expat"]},
        )
        for email in emails:
            c.execute(
                text(
                    "INSERT INTO case_invitation (id, case_id, email, token, status,"
                    " expires_at, created_at, updated_at)"
                    " VALUES (:id, :case, :email, :token, 'pending',"
                    " now() + interval '14 days', now(), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "case": ids["case"],
                    "email": email,
                    "token": str(uuid.uuid4()),
                },
            )


def test_lowercase_emails_roundtrip_and_collision_guard(alembic_db) -> None:
    cfg, engine = alembic_db

    # 1. PARENT + a mixed-case account (the prod incident shape) + the
    # local-db regression: several invitations for one email (same
    # casing) AND case-variants of another — both LEGAL on a non-unique
    # table, neither may trip the guard.
    command.upgrade(cfg, PARENT)
    mixed = _seed_agent(engine, "Contact@DomiBulgarie.COM")
    _seed_case_invitations(
        engine,
        ["marie.dubois@example.com", "marie.dubois@example.com", "Lucas.F@example.com"],
    )

    # 2. THIS: lowercased in place, duplicate invitations untouched.
    command.upgrade(cfg, THIS)
    assert _agent_email(engine, mixed) == "contact@domibulgarie.com"
    with engine.begin() as c:
        invitation_emails = [
            row[0]
            for row in c.execute(text("SELECT email FROM case_invitation ORDER BY email")).all()
        ]
    assert invitation_emails == [
        "lucas.f@example.com",
        "marie.dubois@example.com",
        "marie.dubois@example.com",
    ]

    # 3. Collision guard: back to parent, seed a case-only duplicate pair,
    # the migration refuses (loudly) and leaves the version at PARENT.
    command.downgrade(cfg, PARENT)
    first = _seed_agent(engine, "Dup@Example.com")
    second = _seed_agent(engine, "dup@example.com")
    with pytest.raises(Exception, match="differing only by email case"):
        command.upgrade(cfg, THIS)
    with engine.begin() as c:
        version = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == PARENT

    # 4. Human resolution (drop one), then the migration passes.
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent WHERE id = :id"), {"id": second})
    command.upgrade(cfg, THIS)
    assert _agent_email(engine, first) == "dup@example.com"
