"""Prod seed mode: environment guard (mirror of db-reset), one real
agency with throwaway passwords, idempotent re-run."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.seed import (
    PROD_AGENT_ADMIN,
    PROD_AGENT_ADMIN_2,
    PROD_AGENT_MEMBER,
    PROD_EXPAT_DUPONT,
    PROD_EXPAT_MARTIN,
    PROD_EXPAT_VOLKOV,
    run_seed,
)
from shared.models import Agency, Agent, ExpatUser, Role
from src.core.config import get_settings
from src.core.security import verify_password


async def test_prod_mode_refuses_outside_production(db_session: AsyncSession) -> None:
    assert get_settings().environment != "production"  # harness sets "test"
    with pytest.raises(SystemExit, match="ENVIRONMENT"):
        await run_seed(db_session, "prod")


async def test_unknown_mode_refused(db_session: AsyncSession) -> None:
    with pytest.raises(SystemExit, match="Unknown seed mode"):
        await run_seed(db_session, "banana")


async def test_prod_seed_creates_demo_agency_with_unusable_passwords(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "environment", "production")
    results = await run_seed(db_session, "prod")
    assert len(results) == 3

    agency = (
        await db_session.execute(select(Agency).where(Agency.slug == "nidria-demo"))
    ).scalar_one()
    assert agency.name == "Nidria Demo"

    agents = (
        (await db_session.execute(select(Agent).where(Agent.agency_id == agency.id)))
        .scalars()
        .all()
    )
    assert {a.email for a in agents} == {
        PROD_AGENT_ADMIN,
        PROD_AGENT_ADMIN_2,
        PROD_AGENT_MEMBER,
    }
    by_email = {a.email: a for a in agents}
    assert (by_email[PROD_AGENT_ADMIN_2].first_name, by_email[PROD_AGENT_ADMIN_2].last_name) == (
        "Eric",
        "Schalk",
    )

    # The two founders hold the platform-reserved `superadmin` role (all
    # permissions + agency.create); Membre Démo stays a plain member.
    system_roles = {
        r.name: r
        for r in (await db_session.execute(select(Role).where(Role.is_system))).scalars()
    }
    assert by_email[PROD_AGENT_ADMIN].role_id == system_roles["superadmin"].id
    assert by_email[PROD_AGENT_ADMIN_2].role_id == system_roles["superadmin"].id
    assert by_email[PROD_AGENT_MEMBER].role_id == system_roles["member"].id

    expat_emails = [PROD_EXPAT_MARTIN, PROD_EXPAT_VOLKOV, PROD_EXPAT_DUPONT]
    expats = (
        (await db_session.execute(select(ExpatUser).where(ExpatUser.email.in_(expat_emails))))
        .scalars()
        .all()
    )
    assert len(expats) == 3
    # Personas are named by their role; "Alexandre Montilla" exists
    # exactly ONCE across both identity tables.
    assert {(e.first_name, e.last_name) for e in expats} == {
        ("Client", "Martin"),
        ("Client", "Volkov"),
        ("Client", "Dupont"),
    }
    full_names = [(a.first_name, a.last_name) for a in agents] + [
        (e.first_name, e.last_name) for e in expats
    ]
    assert full_names.count(("Alexandre", "Montilla")) == 1

    # The 6 accounts have no usable password — first login is
    # forgot-password, nothing was printed.
    for account in [*agents, *expats]:
        assert not verify_password("Demo1234!", account.password_hash)
    for expat in expats:
        assert expat.activated_at is not None  # forgot-password works

    # Idempotent: the re-run skips every case block, duplicates nothing.
    rerun = await run_seed(db_session, "prod")
    assert all("skipped" in line for line in rerun)
    count = (
        (await db_session.execute(select(Agent).where(Agent.agency_id == agency.id)))
        .scalars()
        .all()
    )
    assert len(count) == 3


async def test_seed_name_sync_renames_existing_rows(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism that makes a seed rename reach an ALREADY-seeded
    database (prod was seeded with the old names): get-or-create aligns
    the names of seed-owned rows on re-run."""
    monkeypatch.setattr(get_settings(), "environment", "production")
    await run_seed(db_session, "prod")

    agent = (
        await db_session.execute(select(Agent).where(Agent.email == PROD_AGENT_MEMBER))
    ).scalar_one()
    agent.first_name, agent.last_name = "Sasha", "Montilla"  # the pre-rename state
    await db_session.commit()

    await run_seed(db_session, "prod")
    await db_session.refresh(agent)
    assert (agent.first_name, agent.last_name) == ("Membre", "Démo")
