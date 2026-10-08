"""Concurrency and query growth, on synthetic PostgreSQL data only.

Ported from Prism's `tests/test_email_reliability.py` (reference 629a2b0):
- concurrent launches of one campaign create one campaign at Lemlist and
  hand its lead over once (the campaign's row lock);
- concurrent adds of one client into one campaign leave one membership
  (Prism's first test, concurrent duplicate deliveries of an event, belongs
  to phase 4's ingestion and is ported with it);
- the query count of a client record's email section does not grow with
  the number of campaigns it went through (Prism: the contacts page).
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.email_campaign import EmailCampaign, EmailCampaignMember, EmailCampaignStep
from shared.models.rbac import Role
from src.email_campaigns.email_manager import EmailManager
from src.email_campaigns.email_schema import EmailAddMembersRequest, EmailLaunchRequest
from src.email_campaigns.email_sync import EmailSyncManager
from tests.email_fakes import FakeProvider
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import EmailWorld
from tests.test_email_campaigns import campaign_via_api
from tests.test_email_launch import ready_campaign

pytestmark = pytest.mark.usefixtures("rbac_baseline")


async def _agent(db: AsyncSession, agent_id: uuid.UUID) -> Agent:
    """The agent as a request resolves it: its role and permissions loaded."""
    agent = await db.scalar(
        select(Agent)
        .where(Agent.id == agent_id)
        .options(selectinload(Agent.role).selectinload(Role.permissions))
    )
    assert agent is not None
    return agent


async def test_concurrent_launches_create_one_campaign_and_hand_one_lead_over(
    client: AsyncClient,
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, _ = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Concurrent Launch",)
    )
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    async def launch() -> Any:
        async with sessions() as db:
            agent = await _agent(db, email_world.admin_id)
            return await EmailSyncManager(db).launch(agent, uuid.UUID(cid), EmailLaunchRequest())

    results = await asyncio.wait_for(asyncio.gather(*(launch() for _ in range(6))), timeout=30)
    assert sum(result.members_pushed for result in results) == 1
    assert sum(result.campaign_created for result in results) == 1
    names = fake_email_provider.names()
    assert names.count("create_campaign") == 1
    assert names.count("add_contact") == 1 and names.count("launch_contact") == 1
    async with sessions() as db:
        queued = await db.scalar(
            select(func.count())
            .select_from(EmailCampaignMember)
            .where(
                EmailCampaignMember.campaign_id == uuid.UUID(cid),
                EmailCampaignMember.queued_at.is_not(None),
            )
        )
        assert queued == 1


async def test_concurrent_adds_of_one_client_leave_one_membership(
    client: AsyncClient,
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
) -> None:
    person = await make_client_profile(agency_id=email_world.agency_id, email="qa@example.org")
    cid = (await campaign_via_api(client, email_world.headers))["id"]
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    async def add() -> Any:
        async with sessions() as db:
            agent = await _agent(db, email_world.admin_id)
            return await EmailManager(db).add_members(
                agent, uuid.UUID(cid), EmailAddMembersRequest(client_profile_ids=[person.id])
            )

    results = await asyncio.wait_for(asyncio.gather(*(add() for _ in range(6))), timeout=30)
    assert sum(result.added for result in results) == 1
    assert sorted(result.rows[0].eligibility for result in results) == [
        "ALREADY_IN_CAMPAIGN"
    ] * 5 + ["READY"]
    async with sessions() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(EmailCampaignMember)
            .where(EmailCampaignMember.client_profile_id == person.id)
        )
        assert count == 1


async def test_a_client_records_query_count_does_not_grow_with_its_campaigns(
    client: AsyncClient,
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    record_property: Callable[[str, Any], None],
) -> None:
    """One client record that went through forty campaigns must not load
    forty step graphs nor ask once per membership."""
    agency = email_world.agency_id
    one = await make_client_profile(agency_id=agency, email="one@example.org")
    many = await make_client_profile(agency_id=agency, email="many@example.org")
    for index in range(40):
        campaign = EmailCampaign(agency_id=agency, name=f"QA campaign {index}")
        db_session.add(campaign)
        await db_session.flush()
        db_session.add_all(
            EmailCampaignStep(campaign_id=campaign.id, position=position, name=f"Step {position}")
            for position in range(1, 4)
        )
        live = index == 39
        db_session.add(
            EmailCampaignMember(
                agency_id=agency,
                campaign_id=campaign.id,
                client_profile_id=many.id,
                status="EMAIL_READY" if live else "EMAIL_COMPLETED",
                removed_at=None if live or index % 2 else datetime.now(UTC),
            )
        )
        if index == 0:
            db_session.add(
                EmailCampaignMember(
                    agency_id=agency,
                    campaign_id=campaign.id,
                    client_profile_id=one.id,
                    status="EMAIL_READY",
                )
            )
    await db_session.commit()
    queries: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        queries.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record)
    try:
        first = await client.get(f"/email/client-profiles/{one.id}", headers=email_world.headers)
        assert first.status_code == 200, first.text
        one_count = len(queries)
        queries.clear()
        full = await client.get(f"/email/client-profiles/{many.id}", headers=email_world.headers)
        assert full.status_code == 200, full.text
        full_count = len(queries)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record)
    body = full.json()
    assert len(body["memberships"]) == 40 and body["email_status"] == "EMAIL_READY"
    assert all(m["steps_count"] == 3 for m in body["memberships"])
    assert len(first.json()["memberships"]) == 1
    record_property("one_membership_queries", one_count)
    record_property("forty_campaign_queries", full_count)
    assert full_count <= one_count + 3, (one_count, full_count)
