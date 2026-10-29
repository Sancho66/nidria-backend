"""Recover leads launched after import and their already stored history."""

from dataclasses import replace
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import EmailCampaignMember, EmailMessage, EmailProviderEvent
from src.email_campaigns.providers.base import ProviderLeadIdentity
from tests.test_email_auto_import import _auto, _enable_auto
from tests.test_email_campaigns import act, campaign_via_api
from tests.test_email_import import (
    LEMLIST_A,
    LEMLIST_B,
    T0,
    Lead,
    activity,
    campaign_of,
    import_,
    lemlist_campaign,
    members_by_lead,
)
from tests.test_email_outreach import _queue, _window
from tests.test_email_sync_engine import (  # noqa: F401
    Live,
    _run,
    job_db,
    live,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")


async def test_prelaunch_exclusions_are_reconsidered_and_orphan_history_is_applied_once(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    provider, world = live.provider, live.world
    lemlist_campaign(
        provider,
        LEMLIST_A,
        "Started later",
        [
            Lead("lea_later", "later@acme.io", "scanned", "review"),
        ],
    )
    await _enable_auto(db_session)
    await import_(client, world.headers, LEMLIST_A)
    campaign = await campaign_of(db_session, LEMLIST_A)
    assert await members_by_lead(db_session, campaign.id) == {}
    provider.listed[LEMLIST_A][0] = replace(provider.listed[LEMLIST_A][0], state="emailsSent")
    provider.exports[LEMLIST_A] = [
        ProviderLeadIdentity(
            "lea_later", "later@acme.io", status="running", last_state="emailsSent"
        )
    ]
    provider.events = [
        activity("sent_later", "SENT", LEMLIST_A, "lea_later"),
        activity("open_later", "OPENED", LEMLIST_A, "lea_later", at=T0 + timedelta(hours=1)),
        activity("click_later", "CLICKED", LEMLIST_A, "lea_later", at=T0 + timedelta(hours=2)),
    ]
    sync = await _run(live.sessions)
    assert sync.stats["unknown_leads"] == 1
    assert await db_session.scalar(select(func.count()).select_from(EmailProviderEvent)) == 3
    assert await db_session.scalar(select(func.count()).select_from(EmailMessage)) == 0
    recovered = await _auto(live.sessions)
    assert recovered.status == "success", recovered.log_output
    assert recovered.stats["members_created"] == 1
    member = (await members_by_lead(db_session, campaign.id))["lea_later"]
    assert member.status == "EMAIL_ACTIVE" and member.current_step == 1
    messages = (await db_session.scalars(select(EmailMessage))).all()
    assert len(messages) == 1
    assert (messages[0].sent_at, messages[0].open_count, messages[0].click_count) == (T0, 1, 1)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(EmailProviderEvent)
            .where(EmailProviderEvent.campaign_member_id.is_(None))
        )
        == 0
    )
    await import_(client, world.headers, LEMLIST_A)
    await _run(live.sessions)
    await db_session.refresh(messages[0])
    assert (messages[0].open_count, messages[0].click_count) == (1, 1)
    assert await db_session.scalar(select(func.count()).select_from(EmailMessage)) == 1
    assert all(
        name
        in {
            "list_campaigns",
            "read_campaign",
            "read_sequences",
            "list_contacts",
            "export_leads",
            "lookup_contacts",
            "read_activities",
            "sync_campaign",
            "get_messages",
            "get_contact",
        }
        for name in provider.names()
    )


async def test_two_imported_memberships_keep_the_same_person_and_both_histories(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    provider, world = live.provider, live.world
    lemlist_campaign(
        provider, LEMLIST_A, "Old paused", [Lead("lea_old", "one@acme.io", "emailsSent")]
    )
    lemlist_campaign(
        provider,
        LEMLIST_B,
        "New running",
        [Lead("lea_new", "one@acme.io", "emailsSent")],
        status="running",
    )
    provider.events = [
        activity("sent_old", "SENT", LEMLIST_A, "lea_old"),
        activity("sent_new", "SENT", LEMLIST_B, "lea_new", at=T0 + timedelta(days=7)),
    ]
    result = await import_(client, world.headers, LEMLIST_A, LEMLIST_B)
    assert sum(row["members_created"] for row in result["campaigns"]) == 2
    assert sum(row["conflicts"] for row in result["campaigns"]) == 0
    members = (await db_session.scalars(select(EmailCampaignMember))).all()
    assert len(members) == 2
    assert len({m.client_profile_id for m in members}) == 1
    assert {m.status for m in members} == {"EMAIL_ACTIVE"}
    assert await db_session.scalar(select(func.count()).select_from(EmailMessage)) == 2

    # Keeping real provider histories must not authorize a native enrollment.
    target = await campaign_via_api(client, world.headers, "Native target")
    result = await act(
        client, world.headers, "move", [members[0].id], target_campaign_id=target["id"]
    )
    assert result["rows"][0]["code"] == "email.in_other_campaign"
    assert not any(name in {"remove_lead", "pause_lead", "add_leads"} for name in provider.names())


async def test_imported_pending_steps_are_undated_filtered_and_stop_on_unsubscribe(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    provider, world = live.provider, live.world
    lemlist_campaign(
        provider,
        LEMLIST_A,
        "Active mirror",
        [
            Lead("lea_sent", "sent@acme.io", "emailsSent"),
            Lead("lea_next", "next@acme.io", "reviewed"),
            Lead("lea_held", "held@acme.io", "paused", "paused"),
            Lead("lea_review", "review@acme.io", "scanned", "review"),
        ],
        status="running",
        steps=(("email", 0), ("linkedinSend", 1), ("email", 3)),
    )
    provider.events = [activity("history_sent", "SENT", LEMLIST_A, "lea_sent")]
    await import_(client, world.headers, LEMLIST_A)
    campaign = await campaign_of(db_session, LEMLIST_A)
    queue = await _queue(client, world, **_window())
    assert (queue["provider_pending_leads"], queue["provider_pending_emails"]) == (2, 3)
    assert all("day" not in row and "at" not in row for row in queue["provider_pending"])
    assert {
        row["email"]: (row["step_position"], row["remaining_emails"])
        for row in queue["provider_pending"]
    } == {
        "sent@acme.io": (3, 1),
        "next@acme.io": (1, 2),
    }
    assert queue["on_hold"] == 1
    assert not any(row["kind"] in {"SCHEDULED", "OVERDUE"} for row in queue["items"])
    assert (await _queue(client, world, **_window(), search="sent@acme"))[
        "provider_pending_leads"
    ] == 1
    assert (await _queue(client, world, **_window(), kinds=["SENT"]))["provider_pending"] == []
    assert (await _queue(client, world, **_window(), campaign_id=str(world.other_agency_id)))[
        "provider_pending"
    ] == []
    # No repeat import for an unchanged prelaunch exclusion.
    result = await _run(live.sessions)
    assert result.stats["unknown_leads"] == 0
    # A listing gap does not hide an export's unsubscribe (the real s6 case).
    provider.listed[LEMLIST_A] = [
        lead for lead in provider.listed[LEMLIST_A] if lead.lead_id != "lea_next"
    ]
    provider.exports[LEMLIST_A] = [
        replace(row, status=None, state_system="unsubscribed", last_state="variableUnsubscribed")
        if row.lead_id == "lea_next"
        else row
        for row in provider.exports[LEMLIST_A]
    ]
    await _run(live.sessions)
    member = (await members_by_lead(db_session, campaign.id))["lea_next"]
    assert member.status == "EMAIL_UNSUBSCRIBED"
    queue = await _queue(client, world, **_window())
    assert (queue["provider_pending_leads"], queue["provider_pending_emails"]) == (1, 1)
    # A subsequent stale running export never lifts a durable block.
    provider.exports[LEMLIST_A] = [
        replace(row, status="running", state_system="inProgress", last_state="reviewed")
        if row.lead_id == "lea_next"
        else row
        for row in provider.exports[LEMLIST_A]
    ]
    await _run(live.sessions)
    assert (await members_by_lead(db_session, campaign.id))[
        "lea_next"
    ].status == "EMAIL_UNSUBSCRIBED"
