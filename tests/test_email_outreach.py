"""Outreach (spec 8.8, Prism's D7): the agency's activity day by day, what
was reported at its time, and each launched lead's next emails as an
estimated DAY on its campaign's sending days: `schedule`, the one estimate
the member drawer shows too. Leads that cannot be dated (not launched,
paused, a campaign not active, a dry run) are counted, never placed.

Ported from Prism's `tests/test_email_outreach.py` (reference 629a2b0); its
pure estimate tests live in `test_email_schedule.py`. Every query starts
from YESTERDAY: half an hour ago may already be yesterday in Paris (a run
just after midnight), Prism's lesson (c58bfc3). What Lemlist reports goes
through the real ingestion (phase 4), by the performance tests' `ingest`.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import EmailCampaign, EmailCampaignMember
from src.email_campaigns.schedule import today_in
from tests.email_fakes import FakeProvider
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import EmailWorld
from tests.test_email_campaigns import act, set_status
from tests.test_email_launch import add_via_api, launch_via_api, ready_campaign
from tests.test_email_performance import ingest

pytestmark = pytest.mark.usefixtures("rbac_baseline")

PARIS = "Europe/Paris"


async def _queue(client: AsyncClient, world: EmailWorld, **params: Any) -> dict[str, Any]:
    response = await client.get("/email/outreach", params=params, headers=world.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _window(days: int = 14) -> dict[str, str]:
    """From yesterday (never today: see the module docstring), in Paris."""
    today = today_in(PARIS)
    return {
        "start": (today - timedelta(days=1)).isoformat(),
        "end": (today + timedelta(days=days)).isoformat(),
        "tz": PARIS,
    }


async def _drawer(client: AsyncClient, world: EmailWorld, member_id: uuid.UUID) -> dict[str, Any]:
    response = await client.get(f"/email/members/{member_id}", headers=world.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_the_queue_shows_what_happened_and_what_comes_next(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    people = ("Ana Lopez", "Ben Ready", "Cleo Paused")
    cid, _, lead = await ready_campaign(
        client, db_session, email_world, make_client_profile, people, name="Queue"
    )
    await launch_via_api(client, email_world.headers, cid)
    dan = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Dan", last_name="Waiting", email="dan@x.io"
    )
    await add_via_api(client, email_world.headers, cid, client_profile_ids=[dan.id])
    sent_at = datetime.now(UTC) - timedelta(minutes=30)
    await ingest(db_session, lead["Ana Lopez"], [("q1", "SENT", 1, sent_at)])
    await ingest(
        db_session,
        lead["Ben Ready"],
        [("q2", "SENT", 1, sent_at), ("q3", "REPLIED", 1, sent_at + timedelta(minutes=5))],
    )
    paused = await act(client, email_world.headers, "pause", [lead["Cleo Paused"]])
    assert paused["done"] == 1

    queue = await _queue(client, email_world, **_window())
    assert queue["timezone"] == PARIS
    kinds = {(i["full_name"], i["kind"], i["step_position"]) for i in queue["items"]}
    assert {
        ("Ana Lopez", "SENT", 1),
        ("Ben Ready", "SENT", 1),
        ("Ben Ready", "REPLIED", 1),
    } <= kinds
    # Ana's next email is estimated; Ben replied: nothing comes after.
    [next_for_ana] = [i for i in queue["items"] if i["full_name"] == "Ana Lopez" and i["estimated"]]
    assert next_for_ana["kind"] == "SCHEDULED" and next_for_ana["at"] is None
    assert (next_for_ana["step_position"], next_for_ana["step_name"]) == (2, "Relance")
    assert next_for_ana["campaign_name"] == "Queue" and next_for_ana["email"] == "ana@acme.io"
    assert not any(i["full_name"] == "Ben Ready" and i["estimated"] for i in queue["items"])
    # Cleo is paused, Dan not launched: no date for them, counted apart.
    assert not any(i["full_name"] in ("Cleo Paused", "Dan Waiting") for i in queue["items"])
    assert (queue["waiting_launch"], queue["on_hold"], queue["overdue"]) == (1, 1, 0)
    # The past keeps its real time; an estimate has a day only.
    sent = next(i for i in queue["items"] if i["kind"] == "SENT" and i["full_name"] == "Ana Lopez")
    assert sent["at"] is not None and sent["estimated"] is False
    # Ordered by day, then the past before the estimates.
    assert [i["day"] for i in queue["items"]] == sorted(i["day"] for i in queue["items"])

    # The member drawer shows the very same day for Ana's next email.
    step_two = next(
        s
        for s in (await _drawer(client, email_world, lead["Ana Lopez"]))["steps"]
        if s["position"] == 2
    )
    assert step_two["planned_on"] == next_for_ana["day"]
    cleo = await _drawer(client, email_world, lead["Cleo Paused"])
    assert cleo["schedule_hold"] == {"code": "member_paused", "params": {}}
    assert all(s["planned_on"] is None for s in cleo["steps"])

    # Filters, repeated when they take several values.
    replies = await _queue(client, email_world, **_window(), kinds=["REPLIED"])
    assert {i["kind"] for i in replies["items"]} == {"REPLIED"}
    past = await _queue(client, email_world, **_window(), kinds=["SENT", "REPLIED"])
    assert {i["kind"] for i in past["items"]} == {"SENT", "REPLIED"}
    searched = await _queue(client, email_world, **_window(), search="ana@acme")
    assert {i["full_name"] for i in searched["items"]} == {"Ana Lopez"}
    by_status = await _queue(
        client, email_world, **_window(), status=["EMAIL_REPLIED", "EMAIL_PAUSED"]
    )
    assert {i["full_name"] for i in by_status["items"]} == {"Ben Ready"}
    nothing = await _queue(client, email_world, **_window(), campaign_id=str(uuid.uuid4()))
    assert nothing["items"] == []
    assert [c["name"] for c in queue["campaigns"]] == ["Queue"]


async def test_an_overdue_email_shows_today_with_its_estimate(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, lead = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Late",)
    )
    await launch_via_api(client, email_world.headers, cid)
    # Launched ten days ago, never reported sent.
    await db_session.execute(
        update(EmailCampaignMember)
        .where(EmailCampaignMember.id == lead["Ana Late"])
        .values(queued_at=datetime.now(UTC) - timedelta(days=10))
    )
    await db_session.commit()
    today = today_in(PARIS)
    queue = await _queue(
        client, email_world, start=today.isoformat(), end=today.isoformat(), tz=PARIS
    )
    [late] = queue["items"]
    assert late["kind"] == "OVERDUE" and late["day"] == today.isoformat()
    assert late["estimated_day"] < today.isoformat() and late["step_position"] == 1
    # Step 2 counts from today again: only step 1 is overdue.
    assert queue["overdue"] == 1
    # The drawer says the same of the same email.
    step = (await _drawer(client, email_world, lead["Ana Late"]))["steps"][0]
    assert step["planned_overdue"] is True and step["planned_on"] == late["estimated_day"]


async def test_the_queue_is_dated_in_the_viewers_timezone(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, lead = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Night",)
    )
    await launch_via_api(client, email_world.headers, cid)
    # Sent at 23:30 UTC: already the next day in Paris.
    late_evening = datetime(2026, 10, 5, 23, 30, tzinfo=UTC)
    await ingest(db_session, lead["Ana Night"], [("n1", "SENT", 1, late_evening)])
    window = {"start": "2026-10-01", "end": "2026-10-10"}
    paris = await _queue(client, email_world, **window, tz=PARIS)
    utc = await _queue(client, email_world, **window, tz="UTC")
    [in_paris] = [i for i in paris["items"] if i["kind"] == "SENT"]
    [in_utc] = [i for i in utc["items"] if i["kind"] == "SENT"]
    assert (in_paris["day"], in_utc["day"]) == ("2026-10-06", "2026-10-05")
    # An unknown timezone reads as UTC, never an error.
    unknown = await _queue(client, email_world, **window, tz="Mars/Olympus")
    assert unknown["timezone"] == "UTC"


async def test_the_queue_stays_in_its_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    ours, _, lead = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Here",)
    )
    await launch_via_api(client, email_world.headers, ours)
    await ingest(db_session, lead["Ana Here"], [("h1", "SENT", 1, datetime.now(UTC))])
    # The other agency's campaign, launched and sent too.
    theirs = EmailCampaign(
        agency_id=email_world.other_agency_id,
        name="Theirs",
        status="ACTIVE",
        provider_campaign_id="cam_theirs",
        provider_metadata={"team_id": "tea_current"},
    )
    db_session.add(theirs)
    await db_session.commit()
    stranger = await make_client_profile(
        agency_id=email_world.other_agency_id, first_name="Ana", email="ana@elsewhere.io"
    )
    member = EmailCampaignMember(
        campaign_id=theirs.id,
        client_profile_id=stranger.id,
        agency_id=email_world.other_agency_id,
        status="EMAIL_ACTIVE",
        provider_lead_id="lea_theirs",
        queued_at=datetime.now(UTC),
    )
    db_session.add(member)
    await db_session.commit()
    await ingest(db_session, member.id, [("t1", "SENT", 1, datetime.now(UTC))])

    queue = await _queue(client, email_world, **_window())
    assert {i["campaign_id"] for i in queue["items"]} == {ours}
    assert {c["name"] for c in queue["campaigns"]} == {"Founders"}
    # Asking for the other agency's campaign finds nothing here.
    other = await _queue(client, email_world, **_window(), campaign_id=str(theirs.id))
    assert (other["items"], other["waiting_launch"], other["on_hold"]) == ([], 0, 0)
    searched = await _queue(client, email_world, **_window(), search="elsewhere")
    assert searched["items"] == []


async def test_a_period_is_at_most_62_days_and_never_backwards(
    client: AsyncClient, email_world: EmailWorld
) -> None:
    backwards = await client.get(
        "/email/outreach",
        params={"start": "2026-10-10", "end": "2026-10-01"},
        headers=email_world.headers,
    )
    assert (backwards.status_code, backwards.json()["code"]) == (400, "email.invalid_period")
    start = date(2026, 1, 1)
    too_long = await client.get(
        "/email/outreach",
        params={"start": start.isoformat(), "end": (start + timedelta(days=63)).isoformat()},
        headers=email_world.headers,
    )
    assert too_long.status_code == 400
    assert (too_long.json()["code"], too_long.json()["params"]) == (
        "email.period_too_long",
        {"max_days": 62},
    )
    longest = await client.get(
        "/email/outreach",
        params={"start": start.isoformat(), "end": (start + timedelta(days=62)).isoformat()},
        headers=email_world.headers,
    )
    assert longest.status_code == 200
    missing = await client.get("/email/outreach", headers=email_world.headers)
    assert missing.status_code == 422


async def test_the_queue_filters_on_the_sender_chosen_in_nidria(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    sender = {"user_id": "usr_1", "email": "sender@nidria.test"}
    with_sender, _, first = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana One",), name="Sent from"
    )
    await client.patch(
        f"/email/campaigns/{with_sender}",
        json={"settings": {"sender": sender}},
        headers=email_world.headers,
    )
    without, _, second = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ben Two",), name="Default"
    )
    for cid in (with_sender, without):
        await launch_via_api(client, email_world.headers, cid)
    now = datetime.now(UTC)
    await ingest(db_session, first["Ana One"], [("s1", "SENT", 1, now)])
    await ingest(db_session, second["Ben Two"], [("s2", "SENT", 1, now)])
    queue = await _queue(client, email_world, **_window(), sender="sender@nidria.test")
    assert {i["full_name"] for i in queue["items"]} == {"Ana One"}
    assert {i["sender"] for i in queue["items"]} == {"sender@nidria.test"}
    senders = {c["name"]: c["sender"] for c in queue["campaigns"]}
    assert senders == {"Sent from": "sender@nidria.test", "Default": None}


async def test_a_dry_run_lead_is_never_dated(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, lead = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Dry",)
    )
    result = (await launch_via_api(client, email_world.headers, cid)).json()
    assert result["external"] is False
    queue = await _queue(client, email_world, **_window())
    assert queue["items"] == [] and queue["overdue"] == 0
    drawer = await _drawer(client, email_world, lead["Ana Dry"])
    assert drawer["schedule_hold"] == {"code": "dry_run", "params": {}}


async def test_a_paused_campaigns_leads_are_on_hold_not_dated(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, lead = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Held", "Ben Held")
    )
    await launch_via_api(client, email_world.headers, cid)
    await set_status(client, email_world.headers, cid, "PAUSED")
    queue = await _queue(client, email_world, **_window())
    assert queue["items"] == []
    assert (queue["waiting_launch"], queue["on_hold"]) == (0, 2)
    drawer = await _drawer(client, email_world, lead["Ana Held"])
    assert drawer["schedule_hold"] == {
        "code": "campaign_not_active",
        "params": {"status": "PAUSED"},
    }
    campaign = await db_session.get(EmailCampaign, uuid.UUID(cid))
    assert campaign is not None
