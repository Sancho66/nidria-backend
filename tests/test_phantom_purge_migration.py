"""Migration proof for f7a3b9d5c1e8 (phantom external agents purge): a
dead pre-created provider account (cancelled or expired invitation) is
purged with its directory link, while a LIVE claim (pending non-expired
invitation) is kept — on a dedicated testcontainer, real alembic run."""

import os
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from alembic import command

PARENT = "e6f2a8c4d0b3"
THIS = "f7a3b9d5c1e8"


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


def _seed_provider(conn, *, suffix: str, invitation_status: str, expired: bool) -> tuple:
    """agency + external role + pre-created agent + contact + invitation."""
    agency = str(uuid.uuid4())
    role = str(uuid.uuid4())
    agent = str(uuid.uuid4())
    contact = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO agency (id, name, slug, settings) VALUES (:i, :n, :s, '{}'::jsonb)"
        ).bindparams(i=agency, n=f"A {suffix}", s=f"purge-{suffix}")
    )
    conn.execute(
        text(
            "INSERT INTO role (id, name, is_system, is_external) VALUES (:i, :n, true, true)"
        ).bindparams(i=role, n=f"ext_{suffix}")
    )
    conn.execute(
        text(
            "INSERT INTO agent (id, agency_id, role_id, first_name, last_name, "
            "email, password_hash, is_external) "
            "VALUES (:i, :a, :r, 'Ghost', '', :e, 'x', true)"
        ).bindparams(i=agent, a=agency, r=role, e=f"ghost-{suffix}@example.com")
    )
    conn.execute(
        text(
            "INSERT INTO external_contact (id, agency_id, agent_id, name, type) "
            "VALUES (:i, :a, :g, :n, 'other')"
        ).bindparams(i=contact, a=agency, g=agent, n=f"Contact {suffix}")
    )
    conn.execute(
        text(
            "INSERT INTO agent_invitation (id, agency_id, email, role_id, token, "
            "status, external_contact_id, expires_at) "
            "VALUES (:i, :a, :e, :r, :t, :st, :c, "
            "now() + (CASE WHEN :exp THEN interval '-1 day' ELSE interval '14 days' END))"
        ).bindparams(
            i=str(uuid.uuid4()),
            a=agency,
            e=f"ghost-{suffix}@example.com",
            r=role,
            t=f"tok-{suffix}-{uuid.uuid4().hex[:8]}",
            st=invitation_status,
            c=contact,
            exp=expired,
        )
    )
    return agent, contact


def test_purge_drops_dead_phantoms_and_keeps_live_claims(alembic_db) -> None:
    cfg, engine = alembic_db
    command.upgrade(cfg, PARENT)

    with engine.begin() as conn:
        cancelled, c1 = _seed_provider(
            conn, suffix="c", invitation_status="cancelled", expired=False
        )
        expired, c2 = _seed_provider(conn, suffix="e", invitation_status="pending", expired=True)
        alive, c3 = _seed_provider(conn, suffix="a", invitation_status="pending", expired=False)
        accepted, c4 = _seed_provider(
            conn, suffix="ok", invitation_status="accepted", expired=False
        )

    command.upgrade(cfg, THIS)

    with engine.connect() as conn:

        def agent_exists(aid: str) -> bool:
            return conn.execute(
                text("SELECT EXISTS(SELECT 1 FROM agent WHERE id = :i)").bindparams(i=aid)
            ).scalar()

        def contact_link(cid: str):
            return conn.execute(
                text("SELECT agent_id FROM external_contact WHERE id = :i").bindparams(i=cid)
            ).scalar()

        # Dead claims: purged, contact unlinked (re-invitable).
        assert not agent_exists(cancelled) and contact_link(c1) is None
        assert not agent_exists(expired) and contact_link(c2) is None
        # Live claims: untouched.
        assert agent_exists(alive) and contact_link(c3) is not None
        assert agent_exists(accepted) and contact_link(c4) is not None
