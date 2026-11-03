"""Stored replies, complete pagination and tenant isolation for Hot leads."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import EmailCampaignMember
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    EmailWorld,
    MakeEmailCampaign,
    MakeEmailMember,
    MakeEmailMessage,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")


async def reply(
    factory: MakeEmailMessage,
    member: EmailCampaignMember,
    at: datetime,
    **values: Any,
) -> None:
    await factory(
        agency_id=member.agency_id,
        campaign_id=member.campaign_id,
        campaign_member_id=member.id,
        direction="INBOUND",
        status="REPLIED",
        received_at=at,
        content_state="AVAILABLE",
        **values,
    )


async def listing(client: AsyncClient, world: EmailWorld, **params: Any) -> dict[str, Any]:
    response = await client.get("/email/hot-leads", headers=world.headers, params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_latest_reply_per_conversation_keeps_history_and_has_no_sentiment_filter(
    client: AsyncClient,
    email_world: EmailWorld,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_email_message: MakeEmailMessage,
) -> None:
    now = datetime.now(UTC)
    campaign = await make_email_campaign(agency_id=email_world.agency_id, status="ARCHIVED")
    first = await make_email_member(
        campaign_id=campaign.id,
        status="EMAIL_UNSUBSCRIBED",
        removed_at=now,
    )
    second = await make_email_member(agency_id=email_world.agency_id, status="EMAIL_REPLIED")
    # A state or an opening is not a stored answer.
    await make_email_member(agency_id=email_world.agency_id, status="EMAIL_REPLIED")
    await make_email_message(
        agency_id=email_world.agency_id,
        campaign_id=second.campaign_id,
        campaign_member_id=second.id,
        direction="OUTBOUND",
        status="OPENED",
        sent_at=now,
        open_count=2,
    )
    await reply(make_email_message, first, now - timedelta(days=500), body_text="Old reply")
    await reply(make_email_message, second, now - timedelta(hours=1), body_text="Interested")
    await reply(make_email_message, first, now, body_text="Please remove me")
    data = await listing(client, email_world)
    assert data["total"] == 2
    assert [i["member"]["id"] for i in data["items"]] == [str(first.id), str(second.id)]
    latest = data["items"][0]
    assert latest["reply_count"] == 2
    assert latest["latest_reply"]["body_text"] == "Please remove me"
    assert latest["member"]["status"] == "EMAIL_UNSUBSCRIBED"
    assert latest["member"]["removed_at"] is not None


async def test_filters_and_tied_dates_paginate_conversations_without_duplicates(
    client: AsyncClient,
    email_world: EmailWorld,
    make_email_member: MakeEmailMember,
    make_email_message: MakeEmailMessage,
    make_client_profile: MakeClientProfile,
) -> None:
    now = datetime.now(UTC)
    members = []
    for name in ("Anna", "Ben", "Cara"):
        profile = await make_client_profile(
            agency_id=email_world.agency_id, first_name=name, email=f"{name.lower()}@example.test"
        )
        member = await make_email_member(
            agency_id=email_world.agency_id,
            client_profile_id=profile.id,
            provider_metadata={"import": {"company_name": f"{name} Cabinet"}},
        )
        members.append(member)
        await reply(make_email_message, member, now, body_text=name)
        await reply(make_email_message, member, now - timedelta(days=1), body_text="Earlier")
    pages = [await listing(client, email_world, page=n, page_size=1) for n in (1, 2, 3)]
    ids = [p["items"][0]["member"]["id"] for p in pages]
    assert len(set(ids)) == 3
    assert all(p["total"] == 3 and p["page_size"] == 1 for p in pages)
    assert (await listing(client, email_world, page=4, page_size=1))["items"] == []
    for search in ("ben@example", "Ben Cabinet", " Ben "):
        found = await listing(client, email_world, search=search)
        assert found["total"] == 1
        assert found["items"][0]["member"]["id"] == str(members[1].id)
    selected = await listing(client, email_world, campaign_id=str(members[0].campaign_id))
    assert selected["total"] == 1
    assert selected["items"][0]["reply_count"] == 2


async def test_no_cross_agency_messages_or_campaigns(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_email_member: MakeEmailMember,
    make_email_message: MakeEmailMessage,
) -> None:
    ours = await make_email_member(agency_id=email_world.agency_id)
    theirs = await make_email_member(agency_id=email_world.other_agency_id)
    now = datetime.now(UTC)
    await reply(make_email_message, ours, now, body_text="Ours")
    await reply(make_email_message, theirs, now, body_text="Foreign secret")
    # Even inconsistent message ownership must never expose another tenant's text.
    await make_email_message(
        agency_id=theirs.agency_id,
        campaign_id=ours.campaign_id,
        campaign_member_id=ours.id,
        direction="INBOUND",
        status="REPLIED",
        content_state="AVAILABLE",
        received_at=now + timedelta(minutes=1),
        body_text="Wrong message agency",
    )
    data = await listing(client, email_world)
    assert data["total"] == 1
    assert data["items"][0]["reply_count"] == 1
    assert data["items"][0]["latest_reply"]["body_text"] == "Ours"
    assert (await listing(client, email_world, campaign_id=str(theirs.campaign_id)))["total"] == 0
    assert (await listing(client, email_world, search="Foreign secret"))["items"] == []
    # The other agency's own token sees its own conversation.
    other = await client.get("/email/hot-leads", headers=email_world.other_headers)
    assert other.status_code == 200
    assert other.json()["total"] == 1
    assert other.json()["items"][0]["member"]["id"] == str(theirs.id)


async def test_missing_and_html_content_use_the_existing_safe_reply_projection(
    client: AsyncClient,
    email_world: EmailWorld,
    make_email_member: MakeEmailMember,
    make_email_message: MakeEmailMessage,
) -> None:
    now = datetime.now(UTC)
    pending = await make_email_member(agency_id=email_world.agency_id)
    await make_email_message(
        agency_id=pending.agency_id,
        campaign_id=pending.campaign_id,
        campaign_member_id=pending.id,
        direction="INBOUND",
        status="REPLIED",
        received_at=now,
        content_state="PENDING",
    )
    html = await make_email_member(agency_id=email_world.agency_id)
    await reply(
        make_email_message,
        html,
        now - timedelta(minutes=1),
        body_html="<p>Can we talk?</p><script>alert('unsafe')</script>",
    )
    data = await listing(client, email_world)
    assert data["total"] == 2
    assert data["items"][0]["latest_reply"]["body_text"] is None
    assert data["items"][0]["latest_reply"]["content_state"] == "PENDING"
    clean = data["items"][1]["latest_reply"]
    assert "Can we talk?" in clean["body_text"]
    assert "<" not in clean["body_text"]
    assert "body_html" not in clean


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 101},
        {"search": "x" * 201},
        {"campaign_id": "invalid"},
    ],
)
async def test_invalid_filters_are_refused(
    client: AsyncClient,
    email_world: EmailWorld,
    params: dict[str, Any],
) -> None:
    response = await client.get("/email/hot-leads", headers=email_world.headers, params=params)
    assert response.status_code == 422


async def test_empty_unknown_campaign_is_a_valid_empty_result(
    client: AsyncClient,
    email_world: EmailWorld,
) -> None:
    data = await listing(client, email_world, campaign_id=str(uuid.uuid4()))
    assert data == {"items": [], "total": 0, "page": 1, "page_size": 25}
