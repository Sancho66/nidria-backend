"""Prod seed mode: environment guard (mirror of db-reset), one real
agency with throwaway passwords, idempotent re-run."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.seed import (
    PROD_AGENT_ADMIN,
    PROD_AGENT_MEMBER,
    PROD_EXPAT_DUPONT,
    PROD_EXPAT_MARTIN,
    PROD_EXPAT_VOLKOV,
    run_seed,
)
from shared.models import Agency, Agent, ExpatUser
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
    assert {a.email for a in agents} == {PROD_AGENT_ADMIN, PROD_AGENT_MEMBER}

    expat_emails = [PROD_EXPAT_MARTIN, PROD_EXPAT_VOLKOV, PROD_EXPAT_DUPONT]
    expats = (
        (await db_session.execute(select(ExpatUser).where(ExpatUser.email.in_(expat_emails))))
        .scalars()
        .all()
    )
    assert len(expats) == 3

    # The 5 accounts have no usable password — first login is
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
    assert len(count) == 2
