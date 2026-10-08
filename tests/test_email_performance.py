"""Performance (spec 8.7, Prism's D5): Nidria's own figures, all computed in
SQL. A lead is one client in a campaign (a membership), never a message;
rates are over the leads reached, none without any; with a period, the
cohort is the leads launched in it; the daily timeline is in the campaign's
timezone. Lemlist reports no delivery: there is no "delivered".

Ported from Prism's `tests/test_email_performance.py` (reference 629a2b0).
The leads are launched through the API (`FakeProvider`); what Lemlist then
reports goes through phase 4's ingestion, stood in for by `ingest`: one
provider event per activity, its step's OUTBOUND message (sent, opens,
clicks, bounce), an INBOUND message per reply, and the membership's state.
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import EmailCampaignMember, EmailMessage, EmailProviderEvent
from tests.email_fakes import FakeProvider
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import EmailWorld
from tests.test_email_campaigns import campaign_via_api, set_status, step_via_api
from tests.test_email_launch import add_via_api, launch_via_api, ready_campaign

pytestmark = pytest.mark.usefixtures("rbac_baseline")

#: Activity: (event id, type, step position or None, when).
Activity = tuple[str, str, int | None, datetime]


async def ingest(db: AsyncSession, member_id: uuid.UUID, activities: list[Activity]) -> None:
    """What phase 4's ingestion writes for these activities of one lead,
    each stored once (`uq_email_provider_event`)."""
    member = await db.get(EmailCampaignMember, member_id)
    assert member is not None
    for event_id, kind, position, at in activities:
        db.add(
            EmailProviderEvent(
                provider="LEMLIST",
                provider_event_id=event_id,
                agency_id=member.agency_id,
                campaign_id=member.campaign_id,
                campaign_member_id=member.id,
                type=kind,
                provider_type=kind.lower(),
                step_position=position,
                payload={},
                occurred_at=at,
            )
        )
        if kind == "REPLIED":
            db.add(
                EmailMessage(
                    agency_id=member.agency_id,
                    campaign_id=member.campaign_id,
                    campaign_member_id=member.id,
                    step_position=position,
                    provider="LEMLIST",
                    provider_message_id=event_id,
                    direction="INBOUND",
                    status="REPLIED",
                    content_state="PENDING",
                    received_at=at,
                    payload={},
                )
            )
            member.status = "EMAIL_REPLIED"
        elif kind == "UNSUBSCRIBED":
            member.status = "EMAIL_UNSUBSCRIBED"
        if kind in ("SENT", "OPENED", "CLICKED", "BOUNCED", "REPLIED") and position is not None:
            message = await db.scalar(
                select(EmailMessage).where(
                    EmailMessage.campaign_member_id == member.id,
                    EmailMessage.direction == "OUTBOUND",
                    EmailMessage.step_position == position,
                )
            )
            if message is None:
                message = EmailMessage(
                    agency_id=member.agency_id,
                    campaign_id=member.campaign_id,
                    campaign_member_id=member.id,
                    step_position=position,
                    provider="LEMLIST",
                    direction="OUTBOUND",
                    status="SENT",
                    content_state="RENDERED",
                    open_count=0,
                    click_count=0,
                    payload={},
                )
                db.add(message)
            if kind == "SENT":
                message.sent_at = at
                member.started_at = member.started_at or at
                member.current_step = max(member.current_step or 0, position)
                if member.status in ("EMAIL_READY", "EMAIL_QUEUED"):
                    member.status = "EMAIL_ACTIVE"
            elif kind == "OPENED":
                message.open_count += 1
            elif kind == "CLICKED":
                message.click_count += 1
            elif kind == "BOUNCED":
                message.bounced_at = at
                member.status = "EMAIL_BOUNCED"
            elif kind == "REPLIED":
                message.replied_at = at
        await db.flush()
    await db.commit()


async def launched_campaign(
    client: AsyncClient,
    db: AsyncSession,
    world: EmailWorld,
    make_client_profile: MakeClientProfile,
    people: tuple[str, ...],
) -> tuple[str, dict[str, uuid.UUID]]:
    """A two-step campaign whose people are launched for real (the
    provider double): (campaign id, full name -> membership id)."""
    cid, _, members = await ready_campaign(client, db, world, make_client_profile, people)
    launched = await launch_via_api(client, world.headers, cid)
    assert launched.json()["members_pushed"] == len(people), launched.text
    return cid, members


async def _performance(
    client: AsyncClient, world: EmailWorld, cid: str, **params: Any
) -> dict[str, Any]:
    response = await client.get(
        f"/email/campaigns/{cid}/performance", params=params, headers=world.headers
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_leads_and_messages_are_counted_apart(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, lead = await launched_campaign(
        client,
        db_session,
        email_world,
        make_client_profile,
        ("Ana Lopez", "Ben Ready", "Cleo Doe", "Dan Out"),
    )
    t0 = datetime.now(UTC) - timedelta(hours=5)

    def at(minutes: int) -> datetime:
        return t0 + timedelta(minutes=minutes)

    # Ana: two emails, the first opened three times, the second once, then a reply.
    await ingest(
        db_session,
        lead["Ana Lopez"],
        [
            ("a1", "SENT", 1, at(1)),
            ("a2", "OPENED", 1, at(2)),
            ("a3", "OPENED", 1, at(3)),
            ("a4", "OPENED", 1, at(4)),
            ("a5", "SENT", 2, at(10)),
            ("a6", "OPENED", 2, at(11)),
            ("a7", "REPLIED", 2, at(12)),
        ],
    )
    # Ben: one email, opened once, its link clicked twice.
    await ingest(
        db_session,
        lead["Ben Ready"],
        [
            ("b1", "SENT", 1, at(1)),
            ("b2", "OPENED", 1, at(5)),
            ("b3", "CLICKED", 1, at(6)),
            ("b4", "CLICKED", 1, at(7)),
        ],
    )
    # Cleo: bounced. Dan: unsubscribed from the first email.
    await ingest(
        db_session, lead["Cleo Doe"], [("c1", "SENT", 1, at(1)), ("c2", "BOUNCED", 1, at(2))]
    )
    await ingest(
        db_session, lead["Dan Out"], [("d1", "SENT", 1, at(1)), ("d2", "UNSUBSCRIBED", 1, at(3))]
    )

    perf = await _performance(client, email_world, cid)
    assert perf["funnel"] == {
        "launched": 4,
        "launched_dry_run": 0,
        "reached": 4,
        "opened": 2,  # Ana, Ben: four opens on Ana's emails are one lead
        "clicked": 1,
        "replied": 1,
        "bounced": 1,
        "unsubscribed": 1,
    }
    assert perf["rates"] == {
        "open_rate": 0.5,
        "click_rate": 0.25,
        "reply_rate": 0.25,
        "bounce_rate": 0.25,
        "unsubscribe_rate": 0.25,
    }
    assert perf["messages"] == {
        "sent": 5,  # Ana 2, Ben 1, Cleo 1, Dan 1
        "opened": 3,  # Ana's two, Ben's one
        "opens": 5,  # 3 + 1 (Ana) + 1 (Ben)
        "clicked": 1,
        "clicks": 2,
        "replies": 1,
        "bounced": 1,
    }
    assert "delivered" not in perf["funnel"] and "delivered" not in perf["messages"]
    assert (perf["leads_in_campaign"], perf["leads_to_launch"]) == (4, 0)
    assert perf["states"] == {
        "EMAIL_REPLIED": 1,
        "EMAIL_ACTIVE": 1,
        "EMAIL_BOUNCED": 1,
        "EMAIL_UNSUBSCRIBED": 1,
    }
    one, two = perf["steps"]
    assert (one["position"], one["name"]) == (1, "Intro")
    assert {
        k: one[k]
        for k in ("entered", "sent", "opened", "clicked", "replied", "bounced", "unsubscribed")
    } == {
        "entered": 4,
        "sent": 4,
        "opened": 2,
        "clicked": 1,
        "replied": 0,
        "bounced": 1,
        "unsubscribed": 1,
    }
    assert (one["open_rate"], one["bounce_rate"]) == (0.5, 0.25)
    # The reply counts for the step it answers.
    assert {k: two[k] for k in ("entered", "sent", "opened", "replied")} == {
        "entered": 1,
        "sent": 1,
        "opened": 1,
        "replied": 1,
    }
    assert two["reply_rate"] == 1.0 and two["name"] == "Relance"

    # The campaign list says the same of its leads.
    listed = (await client.get("/email/campaigns", headers=email_world.headers)).json()
    stats = next(c for c in listed if c["id"] == cid)["lead_stats"]
    assert stats == {"launched": 4, "reached": 4, "replied": 1}


async def test_an_empty_campaign_has_zero_leads_and_no_rate(
    client: AsyncClient, email_world: EmailWorld
) -> None:
    cid = (await campaign_via_api(client, email_world.headers, "Empty"))["id"]
    await step_via_api(client, email_world.headers, cid, name="Intro")
    perf = await _performance(client, email_world, cid)
    assert all(value == 0 for value in perf["funnel"].values())
    # No lead reached: no rate is a number (the page shows -, never 0%).
    assert all(value is None for value in perf["rates"].values())
    assert all(value == 0 for value in perf["messages"].values())
    [step] = perf["steps"]
    assert step["entered"] == 0 and step["open_rate"] is None
    assert perf["timeline"] == [] and perf["states"] == {}


async def test_a_period_is_the_cohort_of_leads_launched_in_it(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, lead = await launched_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez", "Ben Ready")
    )
    now = datetime.now(UTC)
    await ingest(
        db_session,
        lead["Ana Lopez"],
        [("p1", "SENT", 1, now - timedelta(minutes=2)), ("p2", "REPLIED", 1, now)],
    )
    # Launched today in the campaign's timezone (Europe/Paris): yesterday to
    # tomorrow holds them all, whatever the hour.
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    window = await _performance(
        client,
        email_world,
        cid,
        launched_from=(today - timedelta(days=1)).isoformat(),
        launched_to=(today + timedelta(days=1)).isoformat(),
    )
    assert (window["funnel"]["launched"], window["funnel"]["replied"]) == (2, 1)
    assert window["launched_from"] == (today - timedelta(days=1)).isoformat()
    # Only Ana was sent an email: the rate is over the leads reached (1),
    # not over the leads launched (2).
    assert window["funnel"]["reached"] == 1 and window["rates"]["reply_rate"] == 1.0
    # A period without any launch: an empty cohort, no rate.
    later = await _performance(
        client,
        email_world,
        cid,
        launched_from=(today + timedelta(days=2)).isoformat(),
        launched_to=(today + timedelta(days=9)).isoformat(),
    )
    assert later["funnel"]["launched"] == 0 and later["rates"]["reply_rate"] is None
    assert later["messages"]["sent"] == 0 and later["states"] == {}
    # "Now" figures do not follow the period.
    assert later["leads_in_campaign"] == 2
    backwards = await client.get(
        f"/email/campaigns/{cid}/performance",
        params={
            "launched_from": today.isoformat(),
            "launched_to": (today - timedelta(days=3)).isoformat(),
        },
        headers=email_world.headers,
    )
    assert (backwards.status_code, backwards.json()["code"]) == (400, "email.invalid_period")


async def test_a_dry_run_launch_is_counted_apart(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
) -> None:
    ana = await make_client_profile(agency_id=email_world.agency_id, email="ana@dry.io")
    cid = (await campaign_via_api(client, email_world.headers, "Dry"))["id"]
    await step_via_api(client, email_world.headers, cid)
    await add_via_api(client, email_world.headers, cid, client_profile_ids=[ana.id])
    await set_status(client, email_world.headers, cid, "ACTIVE")
    await launch_via_api(client, email_world.headers, cid)
    perf = await _performance(client, email_world, cid)
    assert (perf["funnel"]["launched"], perf["funnel"]["launched_dry_run"]) == (0, 1)
    assert perf["funnel"]["reached"] == 0 and perf["rates"]["open_rate"] is None


async def test_another_agencys_campaign_has_no_performance_here(
    client: AsyncClient, email_world: EmailWorld
) -> None:
    other = (await campaign_via_api(client, email_world.other_headers, "Elsewhere"))["id"]
    response = await client.get(
        f"/email/campaigns/{other}/performance", headers=email_world.headers
    )
    assert (response.status_code, response.json()["code"]) == (404, "email.campaign_not_found")


async def test_the_timeline_counts_leads_day_by_day_in_the_campaign_timezone(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, lead = await launched_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez", "Ben Ready")
    )
    paris = ZoneInfo("Europe/Paris")
    first = (datetime.now(paris) - timedelta(days=3)).date()

    def local(days: int, hour: int, minute: int = 0) -> datetime:
        moment = datetime.combine(first + timedelta(days=days), time(hour, minute), tzinfo=paris)
        return moment.astimezone(UTC)

    await ingest(
        db_session,
        lead["Ana Lopez"],
        [
            ("t1", "SENT", 1, local(0, 10)),
            ("t3", "OPENED", 1, local(0, 11)),
            ("t4", "OPENED", 1, local(0, 15)),  # Ana again that day: one lead
            ("t7", "REPLIED", 1, local(1, 9)),
        ],
    )
    await ingest(
        db_session,
        lead["Ben Ready"],
        [
            ("t2", "SENT", 1, local(0, 10, 20)),
            # 00:30 in Paris is the next day there (the day before in UTC).
            ("t5", "OPENED", 1, local(1, 0, 30)),
            ("t6", "CLICKED", 1, local(1, 0, 31)),
        ],
    )

    perf = await _performance(client, email_world, cid)
    assert perf["timezone"] == "Europe/Paris"
    timeline = {row["day"]: row for row in perf["timeline"]}
    assert list(timeline) == sorted(timeline)
    day0, day1 = first.isoformat(), (first + timedelta(days=1)).isoformat()
    quiet = dict.fromkeys(
        ("launched", "sent", "opened", "clicked", "replied", "bounced", "unsubscribed"), 0
    )
    assert timeline[day0] == {**quiet, "day": day0, "sent": 2, "opened": 1}
    assert timeline[day1] == {**quiet, "day": day1, "opened": 1, "clicked": 1, "replied": 1}
    today = datetime.now(paris).date().isoformat()
    assert timeline[today]["launched"] == 2

    # A period is a cohort: nobody launched in it, no day either.
    later = (datetime.now(paris) + timedelta(days=2)).date().isoformat()
    assert (await _performance(client, email_world, cid, launched_from=later))["timeline"] == []
