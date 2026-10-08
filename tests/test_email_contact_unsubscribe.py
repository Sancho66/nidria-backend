"""Lemlist's unsubscribes since 30/03/2026: a compliance fix of 08/10/2026.

developer.lemlist.com (webhooks/add-webhook, read 08/10/2026): "Since March
30, 2026, a contact unsubscribing emits `entityUnsubscribed` (whole contact)
or `variableUnsubscribed` (single variable) instead of `emailsUnsubscribed`";
`campaignId` is "absent for non-campaign events", `leadId` and `leadEmail`
are for "campaign events only", `contactId` is on every activity, and the
webhook adds the contact's current record (`email`, ...) to every event
that has one. The real team holds 31 leads in state `entityUnsubscribed`.

Through the ONE ingestion (webhook, job, Sync), with or without a campaign:
- `entityUnsubscribed` is an UNSUBSCRIBED: filed in the agency of its
  campaign, else of the campaign being read, else bound to its `teamId`;
  under the membership of its lead, else of its contact id, else of its
  address; every LIVE membership of the contact (its contact id or its
  addresses) in the agency ends UNSUBSCRIBED, ended ones keep their history,
  and each address goes on the opposition list;
- `variableUnsubscribed` names no documented field saying which variable:
  stored and counted (`variable_unsubscribed`), never applied.

Payloads are Lemlist's documented shape (`_activity`); the job and the Sync
read them through the adapter's own `normalize_activity`. No test reaches
Lemlist: the provider is `FakeProvider`, the guard counts nothing.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agency_email_module import AgencyEmailModule
from shared.models.email_campaign import (
    EmailCampaignMember,
    EmailProviderEvent,
    EmailSuppression,
)
from src.email_campaigns import email_manager
from src.email_campaigns.providers.base import EmailProvider, ProviderEvent
from src.email_campaigns.providers.lemlist import normalize_activity
from src.email_campaigns.suppression import email_hash
from tests.email_fakes import FakeProvider, lemlist_double
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    TEAM_A,
    EmailWorld,
    LemlistTeamDouble,
    MakeEmailCampaign,
    MakeEmailMember,
)
from tests.test_email_campaigns import campaign_via_api, members_of
from tests.test_email_launch import sync_via_api
from tests.test_email_sync_engine import (  # noqa: F401 (fixtures)
    Live,
    _enable_job,
    _lead,
    _live_campaign,
    _member,
    _provider_campaign,
    _run,
    job_db,
    live,
)
from tests.test_email_webhook import SECRET, _launched, configured

pytestmark = pytest.mark.usefixtures("rbac_baseline")

URL = "/webhooks/lemlist"


def _activity(
    event_id: str,
    kind: str,
    *,
    contact: str | None,
    campaign: str | None = None,
    lead: str | None = None,
    email: str | None = None,
    team: str | None = TEAM_A,
    at: datetime | None = None,
) -> dict[str, Any]:
    """One Lemlist activity as documented: the common fields; `campaignId`,
    `leadId`, `leadEmail` only for a campaign event; the contact's current
    record (`email`) on every event that has a `contactId`."""
    body: dict[str, Any] = {
        "_id": event_id,
        "type": kind,
        "createdAt": (at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
    }
    if team is not None:
        body["teamId"] = team
    if contact is not None:
        body["contactId"] = contact
    if campaign is not None:
        body.update(campaignId=campaign, campaignName="Founders")
    if lead is not None:
        body.update(leadId=lead, leadEmail=email, leadFirstName="Ana")
    if email is not None:
        body.update(email=email, firstName="Ana")
    return body


def _event(payload: dict[str, Any]) -> ProviderEvent:
    """What the job and the Sync read: the adapter's own normalization."""
    event = normalize_activity(payload)
    assert event is not None
    return event


async def _bind(db: AsyncSession, agency_id: uuid.UUID, team: str = TEAM_A) -> None:
    await db.execute(
        update(AgencyEmailModule)
        .where(AgencyEmailModule.agency_id == agency_id)
        .values(provider_team_id=team)
    )
    await db.commit()


async def _contact_of(db: AsyncSession, member_id: Any) -> str:
    contact = await db.scalar(
        select(EmailCampaignMember.provider_contact_id).where(
            EmailCampaignMember.id == uuid.UUID(str(member_id))
        )
    )
    assert contact
    return str(contact)


async def _stored(db: AsyncSession, event_id: str) -> EmailProviderEvent | None:
    found: EmailProviderEvent | None = await db.scalar(
        select(EmailProviderEvent)
        .where(EmailProviderEvent.provider_event_id == event_id)
        .execution_options(populate_existing=True)
    )
    return found


async def _listed(db: AsyncSession, agency_id: uuid.UUID) -> dict[str, tuple[str, Any]]:
    rows = await db.execute(
        select(
            EmailSuppression.email_hash,
            EmailSuppression.reason,
            EmailSuppression.source_member_id,
        )
        .where(EmailSuppression.agency_id == agency_id)
        .execution_options(populate_existing=True)
    )
    return {digest: (reason, source) for digest, reason, source in rows.tuples().all()}


async def _status(db: AsyncSession, member_id: Any) -> tuple[str, str | None]:
    row = (
        await db.execute(
            select(EmailCampaignMember.status, EmailCampaignMember.status_reason)
            .where(EmailCampaignMember.id == uuid.UUID(str(member_id)))
            .execution_options(populate_existing=True)
        )
    ).one()
    return row[0], row[1]


async def _post(client: AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(URL, json={**payload, "secret": SECRET})
    assert response.status_code == 200, response.text
    return dict(response.json())


# ── the webhook ──────────────────────────────────────────────────────────────


async def test_a_contacts_unsubscribe_without_a_campaign_reaches_it_in_its_teams_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    fake_email_provider.team = TEAM_A
    await _bind(db_session, email_world.agency_id)
    cid, _, ana = await _launched(client, db_session, email_world, make_client_profile)
    contact = await _contact_of(db_session, ana["id"])
    fake_email_provider.ops.clear()
    payload = _activity("act_u1", "entityUnsubscribed", contact=contact, email="ana@acme.io")
    with configured():
        answer = await _post(client, payload)
        assert answer == {"stored": True, "duplicate": False, "matched": True}
        assert (await _post(client, payload))["duplicate"] is True
    assert await _status(db_session, ana["id"]) == ("EMAIL_UNSUBSCRIBED", None)
    stored = await _stored(db_session, "act_u1")
    assert stored is not None
    assert (stored.agency_id, stored.campaign_id, stored.campaign_member_id) == (
        email_world.agency_id,
        uuid.UUID(cid),
        ana["id"],
    )
    assert (stored.type, stored.provider_type) == ("UNSUBSCRIBED", "entityUnsubscribed")
    assert "secret" not in stored.payload
    assert await _listed(db_session, email_world.agency_id) == {
        email_hash("ana@acme.io"): ("UNSUBSCRIBED", ana["id"])
    }
    # The webhook asks nothing of Lemlist, and writes nothing there.
    assert fake_email_provider.ops == []


async def test_a_contacts_unsubscribe_no_bound_team_can_place_is_not_stored(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """Without a campaign Nidria runs, the agency is the one bound to the
    event's team: an unbound team, or no team said, places it nowhere."""
    _, _, ana = await _launched(client, db_session, email_world, make_client_profile)
    contact = await _contact_of(db_session, ana["id"])
    ignored = {"stored": False, "duplicate": False, "matched": False}
    with configured():
        # The agency is not bound to TEAM_A yet.
        assert await _post(client, _activity("act_1", "entityUnsubscribed", contact=contact)) == (
            ignored
        )
        await _bind(db_session, email_world.agency_id)
        no_team = _activity("act_2", "entityUnsubscribed", contact=contact, team=None)
        assert await _post(client, no_team) == ignored
        other_team = _activity("act_3", "entityUnsubscribed", contact=contact, team="tea_other")
        assert await _post(client, other_team) == ignored
    assert await _stored(db_session, "act_1") is None
    assert await _status(db_session, ana["id"]) == ("EMAIL_QUEUED", None)
    assert await _listed(db_session, email_world.agency_id) == {}


async def test_a_contacts_unsubscribe_ends_every_live_membership_of_the_contact(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
) -> None:
    """Ana is live in Founders (launched), finished an older campaign, and a
    second record carries her address, READY in a third campaign (risk R1).
    Her `entityUnsubscribed`, a campaign event of Founders: her membership
    there, and the second record's live one, end UNSUBSCRIBED; the finished
    campaign keeps its history; her address is listed."""
    cid, lemlist, ana = await _launched(client, db_session, email_world, make_client_profile)
    older = await make_email_campaign(agency_id=email_world.agency_id, name="Older")
    done = await make_email_member(
        campaign_id=older.id,
        client_profile_id=ana["client_profile_id"],
        status="EMAIL_COMPLETED",
    )
    twin = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Ana", email=" ANA@acme.io"
    )
    other = await make_email_campaign(agency_id=email_world.agency_id, name="Other")
    waiting = await make_email_member(campaign_id=other.id, client_profile_id=twin.id)
    contact = await _contact_of(db_session, ana["id"])
    with configured():
        answer = await _post(
            client,
            _activity(
                "act_u2",
                "entityUnsubscribed",
                contact=contact,
                campaign=lemlist,
                lead=ana["provider_lead_id"],
                email="ana@acme.io",
            ),
        )
    assert answer["matched"] is True
    assert await _status(db_session, ana["id"]) == ("EMAIL_UNSUBSCRIBED", None)
    assert await _status(db_session, waiting.id) == (
        "EMAIL_UNSUBSCRIBED",
        "unsubscribed_at_provider",
    )
    assert await _status(db_session, done.id) == ("EMAIL_COMPLETED", None)
    stored = await _stored(db_session, "act_u2")
    assert stored is not None and stored.campaign_id == uuid.UUID(cid)
    listed = await _listed(db_session, email_world.agency_id)
    assert listed == {email_hash("ana@acme.io"): ("UNSUBSCRIBED", ana["id"])}


async def test_a_contacts_unsubscribe_is_placed_by_address_or_listed_unmatched(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """No campaign, a contact id Nidria does not hold: the address places
    it (the contact's current record, `email`). Nobody carries the address:
    stored unmatched, and the address is listed for whoever comes later."""
    fake_email_provider.team = TEAM_A
    await _bind(db_session, email_world.agency_id)
    _, _, ana = await _launched(client, db_session, email_world, make_client_profile)
    with configured():
        by_address = await _post(
            client,
            _activity("act_a", "entityUnsubscribed", contact="ctc_unknown", email="ANA@acme.io"),
        )
        assert by_address == {"stored": True, "duplicate": False, "matched": True}
        nobody = await _post(
            client,
            _activity("act_z", "entityUnsubscribed", contact="ctc_zoe", email="zoe@acme.io"),
        )
        assert nobody == {"stored": True, "duplicate": False, "matched": False}
    assert (await _status(db_session, ana["id"]))[0] == "EMAIL_UNSUBSCRIBED"
    stored = await _stored(db_session, "act_z")
    assert stored is not None
    assert (stored.agency_id, stored.campaign_id, stored.campaign_member_id) == (
        email_world.agency_id,
        None,
        None,
    )
    assert await _listed(db_session, email_world.agency_id) == {
        email_hash("ana@acme.io"): ("UNSUBSCRIBED", ana["id"]),
        email_hash("zoe@acme.io"): ("UNSUBSCRIBED", None),
    }
    zoe = await make_client_profile(agency_id=email_world.agency_id, email="zoe@acme.io")
    following = (await campaign_via_api(client, email_world.headers, "Next"))["id"]
    plan = await client.post(
        f"/email/campaigns/{following}/members/preview-add",
        json={"client_profile_ids": [str(zoe.id)]},
        headers=email_world.headers,
    )
    assert [r["eligibility"] for r in plan.json()["rows"]] == ["UNSUBSCRIBED"]


async def test_a_variables_unsubscribe_is_kept_and_counted_never_applied(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """ "A specific contact variable (email address, phone number, LinkedIn
    profile) was unsubscribed": no documented field says which. Stored under
    the membership it is about, as VARIABLE_UNSUBSCRIBED, nothing applied."""
    fake_email_provider.team = TEAM_A
    await _bind(db_session, email_world.agency_id)
    cid, lemlist, ana = await _launched(client, db_session, email_world, make_client_profile)
    before = await _status(db_session, ana["id"])
    contact = await _contact_of(db_session, ana["id"])
    with configured():
        campaign_event = await _post(
            client,
            _activity(
                "act_v1",
                "variableUnsubscribed",
                contact=contact,
                campaign=lemlist,
                lead=ana["provider_lead_id"],
                email="ana@acme.io",
            ),
        )
        contact_event = await _post(
            client, _activity("act_v2", "variableUnsubscribed", contact=contact)
        )
    assert campaign_event == contact_event == {"stored": True, "duplicate": False, "matched": True}
    assert await _status(db_session, ana["id"]) == before
    for event_id in ("act_v1", "act_v2"):
        stored = await _stored(db_session, event_id)
        assert stored is not None
        assert (stored.type, stored.campaign_member_id, stored.campaign_id) == (
            "VARIABLE_UNSUBSCRIBED",
            ana["id"],
            uuid.UUID(cid),
        )
    assert await _listed(db_session, email_world.agency_id) == {}
    [row] = await members_of(db_session, cid)
    assert (row["status"], before) == ("EMAIL_QUEUED", ("EMAIL_QUEUED", None))


# ── the job and the Sync: the same ingestion ─────────────────────────────────


async def test_the_job_applies_a_contacts_unsubscribe_with_or_without_its_campaign(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
) -> None:
    cid, members = await _live_campaign(
        client,
        make_client_profile,
        live.world,
        people=("Ana Lopez", "Ben Ready", "Cal Doe", "Dee Done"),
    )
    lemlist = await _provider_campaign(db_session, cid)
    ana, ben, cal, dee = [
        await _lead(db_session, members[first]) for first in ("Ana", "Ben", "Cal", "Dee")
    ]
    at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    live.provider.events = [
        _event(
            _activity(
                "act_ana",
                "entityUnsubscribed",
                contact=ana.contact_id,
                campaign=lemlist,
                lead=ana.lead_id,
                email="ana@founders.io",
                at=at,
            )
        ),
        # A contact's event without its campaign, in this campaign's read.
        _event(_activity("act_ben", "entityUnsubscribed", contact=ben.contact_id, at=at)),
        _event(_activity("act_cal", "variableUnsubscribed", contact=cal.contact_id, at=at)),
        _event(
            _activity(
                "act_dee",
                "variableUnsubscribed",
                contact=dee.contact_id,
                campaign=lemlist,
                lead=dee.lead_id,
                at=at,
            )
        ),
    ]
    run = await _run(live.sessions)
    assert run.status == "success", run.log_output
    stats = run.stats
    assert (stats["new_events"], stats["unsubscribed"], stats["variable_unsubscribed"]) == (
        4,
        2,
        2,
    )
    assert "2 variable unsubscribes (not applied)" in run.log_output
    assert (await _member(db_session, ana.member_id)).status == "EMAIL_UNSUBSCRIBED"
    assert (await _member(db_session, ben.member_id)).status == "EMAIL_UNSUBSCRIBED"
    assert (await _member(db_session, cal.member_id)).status != "EMAIL_UNSUBSCRIBED"
    assert (await _member(db_session, dee.member_id)).status != "EMAIL_UNSUBSCRIBED"
    assert await _listed(db_session, live.world.agency_id) == {
        email_hash("ana@founders.io"): ("UNSUBSCRIBED", ana.member_id),
        email_hash("ben@founders.io"): ("UNSUBSCRIBED", ben.member_id),
    }
    again = await _run(live.sessions)
    assert (again.stats["new_events"], again.stats["duplicate_events"]) == (0, 4)
    assert again.stats["variable_unsubscribed"] == 0


async def test_the_sync_applies_a_contacts_unsubscribe_and_the_webhook_meets_it_once(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
    make_client_profile: MakeClientProfile,
) -> None:
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez", "Ben Ready")
    )
    ana, ben = await _lead(db_session, members["Ana"]), await _lead(db_session, members["Ben"])
    payload = _activity("act_s1", "entityUnsubscribed", contact=ben.contact_id)
    live.provider.events = [_event(payload)]
    synced = await sync_via_api(client, live.world.headers, cid)
    assert synced.status_code == 200, synced.text
    assert synced.json()["events_ingested"] == 1
    assert (await _member(db_session, ben.member_id)).status == "EMAIL_UNSUBSCRIBED"
    assert (await _member(db_session, ana.member_id)).status != "EMAIL_UNSUBSCRIBED"
    with configured():
        assert (await _post(client, payload))["duplicate"] is True
    count = await db_session.scalar(
        select(func.count())
        .select_from(EmailProviderEvent)
        .where(EmailProviderEvent.provider_event_id == "act_s1")
    )
    assert count == 1


async def test_the_job_reading_lemlist_itself_applies_every_unsubscribe(
    db_session: AsyncSession,
    email_world: EmailWorld,
    lemlist_live: LemlistTeamDouble,
    job_db: sessionmaker[Session],  # noqa: F811
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end on the real adapter (`FakeLemlist` answering the
    documented routes): the job reads the campaign, its leads (Cal's state
    `entityUnsubscribed`, as 31 leads of the real team today) and its
    activities (Ana's `entityUnsubscribed` with its campaign, Ben's without,
    Dee's `variableUnsubscribed`). Read only toward Lemlist: GET calls only."""
    fake, adapter = lemlist_double(monkeypatch, team_lookup=lambda: TEAM_A)

    def _provider_for(_settings: Any, _row: AgencyEmailModule | None) -> EmailProvider:
        return adapter

    monkeypatch.setattr(email_manager, "provider_for", _provider_for)
    agency = email_world.agency_id
    await _bind(db_session, agency)
    await _enable_job(db_session)
    campaign = await make_email_campaign(
        agency_id=agency,
        name="Founders",
        status="ACTIVE",
        provider_campaign_id="cam_x",
        provider_sequence_id="seq_x",
        provider_metadata={"team_id": TEAM_A},
    )
    members: dict[str, uuid.UUID] = {}
    for first in ("ana", "ben", "cal", "dee"):
        profile = await make_client_profile(
            agency_id=agency, first_name=first.title(), email=f"{first}@founders.io"
        )
        member = await make_email_member(
            campaign_id=campaign.id,
            client_profile_id=profile.id,
            status="EMAIL_ACTIVE",
            provider_lead_id=f"lea_{first}",
            provider_contact_id=f"ctc_{first}",
            queued_at=datetime.now(UTC) - timedelta(days=2),
        )
        members[first] = member.id
    at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    fake.queue("GET", "/campaigns/cam_x", 200, {"_id": "cam_x", "status": "running"})
    fake.queue(
        "GET",
        "/campaigns/cam_x/leads",
        200,
        [
            {"_id": "lea_ana", "contactId": "ctc_ana", "state": "emailsSent"},
            {"_id": "lea_ben", "contactId": "ctc_ben", "state": "emailsSent"},
            {"_id": "lea_cal", "contactId": "ctc_cal", "state": "entityUnsubscribed"},
            {"_id": "lea_dee", "contactId": "ctc_dee", "state": "emailsSent"},
        ],
    )
    fake.queue(
        "GET",
        "/activities",
        200,
        [
            _activity(
                "act_ana",
                "entityUnsubscribed",
                contact="ctc_ana",
                campaign="cam_x",
                lead="lea_ana",
                email="ana@founders.io",
                at=at,
            ),
            _activity("act_ben", "entityUnsubscribed", contact="ctc_ben", at=at),
            _activity(
                "act_dee",
                "variableUnsubscribed",
                contact="ctc_dee",
                campaign="cam_x",
                lead="lea_dee",
                at=at,
            ),
        ],
    )
    run = await _run(job_db)
    assert run.status == "success", run.log_output
    stats = run.stats
    assert (stats["campaigns"], stats["errors"], stats["new_events"]) == (1, 0, 3)
    # Cal from the listing, Ana and Ben from their events; Dee's not applied.
    assert (stats["unsubscribed"], stats["variable_unsubscribed"]) == (3, 1)
    statuses = {first: (await _status(db_session, mid))[0] for first, mid in members.items()}
    assert statuses == {
        "ana": "EMAIL_UNSUBSCRIBED",
        "ben": "EMAIL_UNSUBSCRIBED",
        "cal": "EMAIL_UNSUBSCRIBED",
        "dee": "EMAIL_ACTIVE",
    }
    assert await _listed(db_session, agency) == {
        email_hash(f"{first}@founders.io"): ("UNSUBSCRIBED", members[first])
        for first in ("ana", "ben", "cal")
    }
    assert {method for method, _, _, _ in fake.calls} == {"GET"}
