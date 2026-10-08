"""The Email Sync engine (phase 4): the platform job `email_sync`, the
reconciliation it runs, the messages it keeps, and the ONE ingestion shared
by the job, the webhook and the manual Sync.

Ported from Prism's `tests/test_email_sync_engine.py` (reference 629a2b0):
the job end to end (L245), a reply without content (L402), a broken
campaign (L444), Lemlist-side pauses, unsubscribes and removals (L519), the
detail of a lead (L592), the activity window (L860), and the skips (L816)
adapted to Nidria's platform job (agencies not bound to the key's team,
the dry run without any call). Prism's isolation test (test_email_isolation
L463, each job reads only its project's campaigns) becomes: the job reads
only the bound agency's campaigns, and an activity of another agency's
campaign is never stored. Nidria's own: several agencies, the per-agency
lock, the team unknown, the seed, the registry, the idempotence across the
webhook, the job and the Sync, and NO HAND-OVER by the job or the boot.

No test reaches Lemlist: the agency's provider is `FakeProvider`, the team
lookup is the `GET /team` double, and the network guard counts nothing.
"""

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from shared.models.agency_email_module import AgencyEmailModule
from shared.models.client_profile import ClientProfile
from shared.models.email_campaign import (
    EmailCampaign,
    EmailCampaignMember,
    EmailMessage,
    EmailProviderEvent,
)
from shared.models.job import JobConfig, JobRun
from shared.models.rbac import Role
from src.core.enums import JobTriggeredBy
from src.core.exceptions import NotFoundError
from src.core.job_wrapper import run_job
from src.core.scheduler import JOB_REGISTRY, build_scheduler
from src.email_campaigns import email_sync, sync_job
from src.email_campaigns.email_manager import EmailManager
from src.email_campaigns.email_sync import EmailSyncManager, activity_since
from src.email_campaigns.providers.base import (
    CampaignRef,
    LeadRef,
    ProviderCampaign,
    ProviderError,
    ProviderEvent,
    ProviderMessage,
)
from src.jobs.jobs_baseline import DEFAULT_JOB_CONFIGS, seed_job_configs
from tests import network_guard
from tests.email_fakes import FakeProvider
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    TEAM_A,
    TEAM_B,
    EmailWorld,
    LemlistTeamDouble,
    MakeAgencyEmailModule,
    email_env,
)
from tests.test_email_campaigns import act, campaign_via_api, set_status, step_via_api
from tests.test_email_launch import (
    add_via_api,
    ai_calls,  # noqa: F401 (a fixture)
    launch_via_api,
    sync_via_api,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

SECRET = "s3cret-webhook"
#: What the job may ask of Lemlist: reads only.
READS = {"read_campaign", "list_contacts", "sync_campaign", "get_messages", "get_contact"}
#: What only a Launch may call (spec 8.5).
HAND_OVER = {"add_contact", "launch_contact"}
REPLY_HTML = (
    '<div dir="ltr">Avec plaisir, appelons-nous jeudi.<br><br>Ana</div>'
    '<div class="gmail_quote"><div class="gmail_attr">On Mon, Oct 5, 2026 at 9:00 AM '
    "Nidria &lt;hello@nidria.app&gt; wrote:<br></div><blockquote>Bonjour Ana</blockquote></div>"
)


# ── running the job as production does ──────────────────────────────────────


@pytest.fixture
def job_db(
    async_engine: AsyncEngine,
    sync_session_local: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> sessionmaker[Session]:
    """A sync session for the run (as the scheduler gives it), and the run's
    own async engine in NullPool, here on the test database."""
    monkeypatch.setattr(
        sync_job,
        "make_async_engine",
        lambda _settings: create_async_engine(async_engine.url, poolclass=NullPool),
    )
    return sync_session_local


async def _enable_job(db: AsyncSession) -> None:
    """The seeded row is disabled (R9: run_job skips a disabled job, even a
    manual run): switched on the way go-live does it."""
    await seed_job_configs(db)
    await db.execute(
        update(JobConfig).where(JobConfig.job_id == "email_sync").values(is_enabled=True)
    )
    await db.commit()


async def _run(sessions: sessionmaker[Session], *, dry_run: bool = False) -> JobRun:
    """One run through the job wrapper, in a worker thread like the scheduler's."""

    def go() -> JobRun:
        with sessions() as db:
            return run_job(
                db,
                job_id="email_sync",
                pipeline=JOB_REGISTRY["email_sync"],
                triggered_by=JobTriggeredBy.MANUAL,
                dry_run=dry_run,
            )

    return await asyncio.to_thread(go)


async def _bind(db: AsyncSession, agency_id: uuid.UUID, team: str | None = TEAM_A) -> None:
    await db.execute(
        update(AgencyEmailModule)
        .where(AgencyEmailModule.agency_id == agency_id)
        .values(provider_team_id=team)
    )
    await db.commit()


@dataclass
class Live:
    """The agency bound to the key's team (TEAM_A), real effects on, its
    provider the `FakeProvider` (whose team is the key's)."""

    world: EmailWorld
    provider: FakeProvider
    team: LemlistTeamDouble
    sessions: sessionmaker[Session]


@pytest_asyncio.fixture
async def live(
    db_session: AsyncSession,
    email_world: EmailWorld,
    lemlist_live: LemlistTeamDouble,
    fake_email_provider: FakeProvider,
    job_db: sessionmaker[Session],
) -> Live:
    fake_email_provider.team = TEAM_A
    await _bind(db_session, email_world.agency_id)
    await _enable_job(db_session)
    return Live(email_world, fake_email_provider, lemlist_live, job_db)


async def _live_campaign(
    client: AsyncClient,
    make_client_profile: MakeClientProfile,
    world: EmailWorld,
    *,
    name: str = "Founders",
    people: tuple[str, ...] = ("Ana Lopez", "Ben Ready"),
    steps: int = 3,
    ai: bool = False,
    other_agency: bool = False,
) -> tuple[str, dict[str, uuid.UUID]]:
    """A live campaign of `steps` emails, its people launched (a Launch is
    the only hand-over): (campaign id, first name -> membership id)."""
    headers = world.other_headers if other_agency else world.headers
    agency_id = world.other_agency_id if other_agency else world.agency_id
    profiles: dict[str, uuid.UUID] = {}
    for person in people:
        first, last = person.split()
        profile = await make_client_profile(
            agency_id=agency_id,
            first_name=first,
            last_name=last,
            email=f"{first.lower()}@{name.lower()}.io",
        )
        profiles[first] = profile.id
    cid = (await campaign_via_api(client, headers, name))["id"]
    detail = await step_via_api(client, headers, cid, name="Email initial")
    if ai:
        step_id = detail["steps"][0]["id"]
        created = await client.post(
            f"/email/steps/{step_id}/ai-variables",
            json={"key": "hook", "prompt_template": "Une phrase sur {{client.first_name}}."},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        patched = await client.patch(
            f"/email/steps/{step_id}",
            json={"body_template": "Bonjour {{client.first_name}},\n\n{{ai.hook}}"},
            headers=headers,
        )
        assert patched.status_code == 200, patched.text
    for position in range(2, steps + 1):
        await step_via_api(
            client,
            headers,
            cid,
            name=f"Relance {position - 1}",
            subject_template="",
            body_template=f"Relance {position - 1}.",
        )
    await add_via_api(client, headers, cid, client_profile_ids=list(profiles.values()))
    await set_status(client, headers, cid, "ACTIVE")
    launched = await launch_via_api(client, headers, cid)
    assert launched.status_code == 200, launched.text
    assert launched.json()["members_pushed"] == len(people), launched.json()
    rows = await _members(client, headers, cid)
    by_profile = {row["client_profile_id"]: row["id"] for row in rows}
    return cid, {first: uuid.UUID(by_profile[str(pid)]) for first, pid in profiles.items()}


async def _members(client: AsyncClient, headers: dict[str, str], cid: str) -> list[dict[str, Any]]:
    response = await client.get(
        "/email/members", params={"campaign_id": cid, "page_size": 200}, headers=headers
    )
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


@dataclass(frozen=True)
class Lead:
    member_id: uuid.UUID
    lead_id: str
    contact_id: str | None


async def _lead(db: AsyncSession, member_id: uuid.UUID) -> Lead:
    row = (
        await db.execute(
            select(
                EmailCampaignMember.provider_lead_id, EmailCampaignMember.provider_contact_id
            ).where(EmailCampaignMember.id == member_id)
        )
    ).one()
    assert row.provider_lead_id
    return Lead(member_id, row.provider_lead_id, row.provider_contact_id)


async def _member(db: AsyncSession, member_id: uuid.UUID) -> EmailCampaignMember:
    member = await db.scalar(
        select(EmailCampaignMember)
        .where(EmailCampaignMember.id == member_id)
        .execution_options(populate_existing=True)
    )
    assert member is not None
    return member


async def _campaign(db: AsyncSession, cid: str) -> EmailCampaign:
    campaign = await db.scalar(
        select(EmailCampaign)
        .where(EmailCampaign.id == uuid.UUID(cid))
        .execution_options(populate_existing=True)
    )
    assert campaign is not None
    return campaign


async def _provider_campaign(db: AsyncSession, cid: str) -> str:
    value = (await _campaign(db, cid)).provider_campaign_id
    assert value
    return value


async def _messages(db: AsyncSession, member_id: uuid.UUID) -> dict[str, EmailMessage]:
    rows = await db.execute(
        select(EmailMessage)
        .where(EmailMessage.campaign_member_id == member_id)
        .execution_options(populate_existing=True)
    )
    found: dict[str, EmailMessage] = {}
    for message in rows.scalars().all():
        key = (
            f"out{message.step_position}"
            if message.direction == "OUTBOUND"
            else f"in{message.provider_message_id}"
        )
        found[key] = message
    return found


def _event(
    event_id: str,
    kind: str,
    campaign: str,
    lead: str | None,
    step: int | None = None,
    *,
    reason: str | None = None,
    at: datetime | None = None,
    **payload: Any,
) -> ProviderEvent:
    return ProviderEvent(
        event_id=event_id,
        type=kind,
        provider_type=f"emails{kind.title()}",
        occurred_at=at or datetime.now(UTC),
        campaign_id=campaign,
        lead_id=lead,
        email=None,
        step_position=step,
        reason=reason,
        payload={"_id": event_id, **payload},
    )


def _reply(
    event_id: str, campaign: str, lead: Lead, at: datetime, *, step: int = 1
) -> ProviderEvent:
    return ProviderEvent(
        event_id=event_id,
        type="REPLIED",
        provider_type="emailsReplied",
        occurred_at=at,
        campaign_id=campaign,
        lead_id=lead.lead_id,
        email=None,
        step_position=step,
        reason=None,
        payload={"_id": event_id, "contactId": lead.contact_id, "subject": "Re: une question"},
    )


def _inbox_reply(
    message_id: str, campaign: str, lead: Lead, at: datetime, html: str, **payload: Any
) -> ProviderMessage:
    return ProviderMessage(
        message_id=message_id,
        direction="INBOUND",
        occurred_at=at,
        campaign_id=campaign,
        lead_id=lead.lead_id,
        contact_id=lead.contact_id,
        step_position=1,
        subject="Re: une question",
        body_html=html,
        payload={"_id": message_id, "type": "emailsReplied", **payload},
    )


@contextmanager
def _webhook_secret() -> Iterator[None]:
    with email_env(EMAIL_WEBHOOK_SECRET=SECRET):
        yield


# ── the job, end to end (Prism L245) ─────────────────────────────────────────


async def test_the_job_reconciles_messages_and_replies_and_never_counts_twice(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
) -> None:
    provider = live.provider
    cid, members = await _live_campaign(client, make_client_profile, live.world)
    lemlist = await _provider_campaign(db_session, cid)
    ana, ben = await _lead(db_session, members["Ana"]), await _lead(db_session, members["Ben"])
    # What Lemlist says of the campaign: its words are kept, Nidria's team
    # note is not overwritten, and its name there changes nothing here.
    provider.remote[lemlist] = ProviderCampaign(
        lemlist,
        exists=True,
        status="running",
        metadata={"status": "running", "name": "Renamed in Lemlist", "team_id": "tea_other"},
    )
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
    provider.events = [
        _event("s1", "SENT", lemlist, ana.lead_id, 1, at=t0),
        _event("o1", "OPENED", lemlist, ana.lead_id, 1, at=t0 + timedelta(minutes=10)),
        _event("o2", "OPENED", lemlist, ana.lead_id, 1, at=t0 + timedelta(minutes=20)),
        _event("c1", "CLICKED", lemlist, ana.lead_id, 1, at=t0 + timedelta(minutes=25)),
        _reply("r1", lemlist, ana, t0 + timedelta(minutes=30)),
        _event("s2", "SENT", lemlist, ben.lead_id, 1, at=t0 + timedelta(minutes=5)),
        _event(
            "b1",
            "BOUNCED",
            lemlist,
            ben.lead_id,
            1,
            reason="550 No such user",
            at=t0 + timedelta(minutes=6),
        ),
    ]
    assert ana.contact_id is not None
    provider.inbox[ana.contact_id] = [
        _inbox_reply("r1", lemlist, ana, t0 + timedelta(minutes=30), REPLY_HTML)
    ]
    before = len(provider.ops)

    run = await _run(live.sessions)
    assert run.status == "success", run.log_output
    stats = run.stats
    assert (stats["campaigns"], stats["contacts"], stats["new_events"]) == (1, 2, 7)
    assert (stats["sent"], stats["opened"], stats["clicked"]) == (2, 2, 1)
    assert (stats["replies"], stats["bounces"], stats["errors"]) == (1, 1, 0)
    assert (stats["reply_contents"], stats["error_messages"]) == (1, [])
    # The other agency of the world has the module on but no team: skipped.
    assert (stats["agencies"], stats["agencies_skipped"]) == (1, 1)
    assert "\u2014" not in run.log_output
    # Read only toward the provider: nothing pushed, launched or generated.
    assert {name for name, _ in provider.ops[before:]} == {
        "read_campaign",
        "list_contacts",
        "sync_campaign",
        "get_messages",
    }

    messages = await _messages(db_session, ana.member_id)
    out = messages["out1"]
    assert (out.status, out.open_count, out.click_count) == ("REPLIED", 2, 1)
    assert (out.first_opened_at, out.last_opened_at) == (
        t0 + timedelta(minutes=10),
        t0 + timedelta(minutes=20),
    )
    assert (out.provider_message_id, out.sent_at, out.content_state) == ("s1", t0, "RENDERED")
    assert out.render_id is not None and out.provider_thread_id == ana.contact_id
    reply = messages["inr1"]
    assert (reply.direction, reply.status, reply.content_state) == (
        "INBOUND",
        "REPLIED",
        "AVAILABLE",
    )
    assert reply.body_text == "Avec plaisir, appelons-nous jeudi.\n\nAna"  # the quote is gone
    assert reply.body_html is not None and reply.body_html.startswith('<div dir="ltr">')
    assert (await _messages(db_session, ben.member_id))["out1"].status == "BOUNCED"

    ana_row = await _member(db_session, ana.member_id)
    assert ana_row.status == "EMAIL_REPLIED"
    assert ana_row.last_opened_at == t0 + timedelta(minutes=20)
    assert ana_row.last_replied_at == t0 + timedelta(minutes=30)
    assert ana_row.provider_status == "emailsSent"  # the provider's own word, kept
    ben_row = await _member(db_session, ben.member_id)
    assert (ben_row.status, ben_row.status_reason) == ("EMAIL_BOUNCED", "bounced")
    assert ben_row.provider_metadata["status_reason_params"] == {"reason": "550 No such user"}
    # No business status is written by an email event (spec 8.9).
    overrides = await db_session.scalars(
        select(ClientProfile.status_override).where(ClientProfile.agency_id == live.world.agency_id)
    )
    assert set(overrides.all()) == {None}

    campaign = await _campaign(db_session, cid)
    assert campaign.provider_status == "running" and campaign.last_provider_error is None
    assert campaign.last_activity_sync_at is not None and campaign.last_provider_sync_at
    # Nidria's own decisions are not the provider's to change.
    assert (campaign.status, campaign.name) == ("ACTIVE", "Founders")
    assert campaign.provider_metadata == {
        "status": "running",
        "name": "Renamed in Lemlist",
        "team_id": TEAM_A,
    }
    last = await db_session.scalar(
        select(JobConfig.last_run_status)
        .where(JobConfig.job_id == "email_sync")
        .execution_options(populate_existing=True)
    )
    assert last == "success"

    # The same run again: nothing new, nothing counted twice; the read starts
    # at the checkpoint minus the overlap.
    checkpoint = campaign.last_activity_sync_at
    again = await _run(live.sessions)
    assert again.status == "success"
    assert (again.stats["new_events"], again.stats["duplicate_events"]) == (0, 7)
    assert provider.since[-1] == activity_since(checkpoint, datetime.now(UTC))
    assert (await _messages(db_session, ana.member_id))["out1"].open_count == 2
    assert await db_session.scalar(select(func.count()).select_from(EmailMessage)) == 3

    # Webhook and job meet on the same events: each is stored and counted once.
    hook = {
        "_id": "o2",
        "type": "emailsOpened",
        "createdAt": (t0 + timedelta(minutes=20)).isoformat(),
        "campaignId": lemlist,
        "leadId": ana.lead_id,
        "sequenceStep": 0,
        "secret": SECRET,
    }
    with _webhook_secret():
        replayed = (await client.post("/webhooks/lemlist", json=hook)).json()
        assert replayed == {"stored": False, "duplicate": True, "matched": False}
        fresh = {**hook, "_id": "o3", "createdAt": (t0 + timedelta(hours=1)).isoformat()}
        assert (await client.post("/webhooks/lemlist", json=fresh)).json()["stored"] is True
    provider.events.append(
        _event("o3", "OPENED", lemlist, ana.lead_id, 1, at=t0 + timedelta(hours=1))
    )
    third = await _run(live.sessions)
    assert (third.stats["new_events"], third.stats["duplicate_events"]) == (0, 8)
    out = (await _messages(db_session, ana.member_id))["out1"]
    assert (out.open_count, out.last_opened_at) == (3, t0 + timedelta(hours=1))
    assert await db_session.scalar(select(func.count()).select_from(EmailProviderEvent)) == 8


async def test_a_reply_without_content_stays_replied_and_says_so(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez",)
    )
    lemlist = await _provider_campaign(db_session, cid)
    ana = await _lead(db_session, members["Ana"])
    at = datetime.now(UTC) - timedelta(hours=1)
    live.provider.events = [
        _event("s1", "SENT", lemlist, ana.lead_id, 1, at=at - timedelta(days=1)),
        _reply("r1", lemlist, ana, at),
    ]
    # The inbox knows the thread but not this reply: no content is invented.
    for attempt in range(3):
        run = await _run(live.sessions)
        assert run.status == "success"
        reply = (await _messages(db_session, ana.member_id))["inr1"]
        assert reply.content_attempts == attempt + 1
    assert (reply.content_state, reply.body_text, reply.status) == ("UNAVAILABLE", None, "REPLIED")
    [membership] = await _members(client, live.world.headers, cid)
    assert membership["status"] == "EMAIL_REPLIED"
    detail = (
        await client.get(f"/email/members/{membership['id']}", headers=live.world.headers)
    ).json()
    [step_reply] = detail["steps"][0]["replies"]
    assert (step_reply["content_state"], step_reply["body_text"]) == ("UNAVAILABLE", None)
    assert [c["direction"] for c in detail["conversation"]] == ["OUTBOUND", "INBOUND"]


async def test_one_broken_campaign_does_not_stop_the_others(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
) -> None:
    provider = live.provider
    broken, _ = await _live_campaign(
        client, make_client_profile, live.world, name="Broken", people=("Ana Lopez",)
    )
    deleted, _ = await _live_campaign(
        client, make_client_profile, live.world, name="Deleted", people=("Cal Doe",)
    )
    healthy, members = await _live_campaign(
        client, make_client_profile, live.world, name="Healthy", people=("Ben Ready",)
    )
    broken_ref = await _provider_campaign(db_session, broken)
    deleted_ref = await _provider_campaign(db_session, deleted)
    healthy_ref = await _provider_campaign(db_session, healthy)
    before = {cid: (await _campaign(db_session, cid)).last_activity_sync_at for cid in (broken,)}
    provider.failures[("sync_campaign", broken_ref)] = ProviderError(
        "Lemlist: Internal Server Error", code="PROVIDER_ERROR", status=500
    )
    provider.remote[deleted_ref] = ProviderCampaign(deleted_ref, exists=False)
    ben = await _lead(db_session, members["Ben"])
    provider.events = [_event("s9", "SENT", healthy_ref, ben.lead_id, 1)]

    run = await _run(live.sessions)
    # Decision A6: the wrapper knows no "partial"; the stats say what failed.
    assert run.status == "success", run.log_output
    stats = run.stats
    assert (stats["campaigns"], stats["campaigns_failed"], stats["errors"]) == (3, 2, 2)
    assert stats["new_events"] == 1 and stats["sent"] == 1
    assert {(e["code"], e["campaign_id"]) for e in stats["error_messages"]} == {
        ("email.provider_error", broken),
        ("email.provider_campaign_missing", deleted),
    }
    assert all(e["agency_id"] == str(live.world.agency_id) for e in stats["error_messages"])
    rows = {cid: await _campaign(db_session, cid) for cid in (broken, deleted, healthy)}
    # The failing campaigns keep their checkpoint: the next run reads the
    # same window again. The healthy one moved on.
    assert rows[broken].last_activity_sync_at == before[broken]
    assert rows[broken].last_provider_error == "email.provider_error"
    assert (rows[deleted].provider_status, rows[deleted].status) == ("missing", "ACTIVE")
    assert rows[deleted].last_provider_error == "email.provider_campaign_missing"
    healthy_at = rows[healthy].last_activity_sync_at
    assert healthy_at is not None and before[broken] is not None
    assert healthy_at > before[broken]
    assert rows[healthy].last_provider_error is None
    assert (await _member(db_session, ben.member_id)).status == "EMAIL_ACTIVE"

    # Everything failing: every campaign reported failed, the run still ends.
    provider.failures[("read_campaign", healthy_ref)] = ProviderError("down", code="TIMEOUT")
    failed = await _run(live.sessions)
    assert failed.status == "success"
    assert failed.stats["campaigns_failed"] == failed.stats["campaigns"] == 3


async def test_lemlist_side_pauses_unsubscribes_removals_and_completion(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
) -> None:
    provider = live.provider
    cid, members = await _live_campaign(
        client,
        make_client_profile,
        live.world,
        people=("Ana Lopez", "Ben Ready", "Cal Doe", "Dee Done", "Eve Full"),
        steps=2,
    )
    lemlist = await _provider_campaign(db_session, cid)
    ana, ben, cal, dee, eve = [
        await _lead(db_session, members[first]) for first in ("Ana", "Ben", "Cal", "Dee", "Eve")
    ]
    provider.paused.add(ana.lead_id)
    provider.unsubscribed.add(ben.lead_id)
    provider.gone.add(cal.lead_id)
    provider.events = [
        _event("d1", "SENT", lemlist, dee.lead_id, 1),
        _event("d2", "SENT", lemlist, dee.lead_id, 2),
        _event("f1", "FAILED", lemlist, eve.lead_id, 1, reason="Mailbox full"),
    ]
    run = await _run(live.sessions)
    assert run.status == "success"
    stats = run.stats
    assert (stats["pause_changes"], stats["missing_leads"], stats["unsubscribed"]) == (1, 1, 1)
    rows = {lead.member_id: await _member(db_session, lead.member_id) for lead in (ana, ben, cal)}
    assert (rows[ana.member_id].status, rows[ana.member_id].status_reason) == (
        "EMAIL_PAUSED",
        "paused_at_provider",
    )
    assert rows[ana.member_id].paused_by_agent_id is None  # nobody in Nidria paused her
    assert (rows[ben.member_id].status, rows[ben.member_id].status_reason) == (
        "EMAIL_UNSUBSCRIBED",
        "unsubscribed_at_provider",
    )
    # Absent from Lemlist's leads: flagged, never ended on an inference.
    assert rows[cal.member_id].status == "EMAIL_QUEUED"
    assert rows[cal.member_id].provider_status == "missing"
    assert rows[cal.member_id].status_reason == "missing_at_provider"
    assert (await _member(db_session, dee.member_id)).status == "EMAIL_COMPLETED"
    eve_row = await _member(db_session, eve.member_id)
    assert (eve_row.status, eve_row.status_reason) == ("EMAIL_QUEUED", "send_failed")

    # Resumed in Lemlist, back in Lemlist: mirrored, the flag cleared; the
    # provider's metadata is merged, never over Nidria's reason params.
    provider.paused.discard(ana.lead_id)
    provider.gone.discard(cal.lead_id)
    await _run(live.sessions)
    cal_row = await _member(db_session, cal.member_id)
    assert (cal_row.status_reason, cal_row.provider_status) == (None, "emailsSent")
    assert (await _member(db_session, ana.member_id)).status == "EMAIL_QUEUED"
    eve_row = await _member(db_session, eve.member_id)
    assert eve_row.provider_metadata["status_reason_params"] == {"reason": "Mailbox full"}
    assert eve_row.provider_metadata["contactId"] == eve.contact_id


async def test_the_detail_shows_each_step_its_content_ai_values_events_and_reply(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
    ai_calls: list[str],  # noqa: F811
) -> None:
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez",), steps=3, ai=True
    )
    lemlist = await _provider_campaign(db_session, cid)
    ana = await _lead(db_session, members["Ana"])
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(days=5)
    live.provider.events = [
        _event("s1", "SENT", lemlist, ana.lead_id, 1, at=t0),
        _event("o1", "OPENED", lemlist, ana.lead_id, 1, at=t0 + timedelta(hours=1)),
        _event("s2", "SENT", lemlist, ana.lead_id, 2, at=t0 + timedelta(days=3)),
        _event("o2", "OPENED", lemlist, ana.lead_id, 2, at=t0 + timedelta(days=3, hours=1)),
        _event("o3", "OPENED", lemlist, ana.lead_id, 2, at=t0 + timedelta(days=3, hours=2)),
        _reply("r1", lemlist, ana, t0 + timedelta(days=3, hours=3), step=2),
    ]
    assert ana.contact_id is not None
    live.provider.inbox[ana.contact_id] = [
        _inbox_reply("r1", lemlist, ana, t0 + timedelta(days=3, hours=3), REPLY_HTML)
    ]
    assert (await _run(live.sessions)).status == "success"
    assert ai_calls == ["hook"]  # at the Launch only: the job generates nothing

    [row] = await _members(client, live.world.headers, cid)
    # The list carries no email bodies; the detail is read on demand.
    assert "steps" not in row and "conversation" not in row
    assert row["last_replied_at"] is not None and row["status"] == "EMAIL_REPLIED"
    detail = (await client.get(f"/email/members/{row['id']}", headers=live.world.headers)).json()
    first, second, third = detail["steps"]
    assert (first["status"], first["sent_at"][:19], first["open_count"]) == (
        "OPENED",
        t0.isoformat()[:19],
        1,
    )
    assert first["subject"] == "Ana, une question"
    assert first["body"].startswith("Bonjour Ana,")
    assert first["ai_values"] == {"hook": "Une phrase."}
    assert [e["type"] for e in first["events"]] == ["OPENED", "SENT"]
    assert (second["status"], second["open_count"]) == ("REPLIED", 2)
    assert second["body"] == "Relance 1."
    [reply] = second["replies"]
    assert reply["content_state"] == "AVAILABLE"
    assert reply["body_text"].startswith("Avec plaisir")
    # Not sent, and never: she replied, which ended her sequence; no date is
    # shown for it (D7), the detail says why.
    assert (third["status"], third["sent_at"], third["planned_on"]) == ("QUEUED", None, None)
    assert detail["schedule_hold"] == {"code": "sequence_ended", "params": {"reason": "replied"}}
    assert [(c["direction"], c["step_position"]) for c in detail["conversation"]] == [
        ("OUTBOUND", 1),
        ("OUTBOUND", 2),
        ("INBOUND", 2),
    ]
    assert detail["conversation"][2]["body"].startswith("Avec plaisir")
    assert {e["type"] for e in detail["events"]} == {"SENT", "OPENED", "REPLIED"}

    # Campaign counters come from Nidria's synced messages.
    campaign = (await client.get(f"/email/campaigns/{cid}", headers=live.world.headers)).json()
    assert campaign["metrics"] == {"sent": 2, "opened": 2, "clicked": 0, "replied": 1, "bounced": 0}
    assert campaign["metrics_by_step"] == {
        "1": {"sent": 1, "opened": 1, "clicked": 0, "replied": 0, "bounced": 0},
        "2": {"sent": 1, "opened": 1, "clicked": 0, "replied": 1, "bounced": 0},
    }
    assert campaign["provider_status"] == "running"


def test_the_activity_window_overlaps_the_checkpoint() -> None:
    now = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
    assert activity_since(None, now) is None  # first read: everything
    assert activity_since(now - timedelta(minutes=10), now) == now - timedelta(minutes=70)
    # The first read of a UTC day reaches a day back: late activities are kept.
    yesterday = datetime(2026, 10, 3, 23, 55, tzinfo=UTC)
    assert activity_since(yesterday, now) == yesterday - timedelta(days=1)


# ── skipped: the dry run calls nothing (Prism L816, adapted) ─────────────────


async def test_a_dry_run_or_effects_off_is_skipped_without_any_call(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    lemlist_team_double: LemlistTeamDouble,
    job_db: sessionmaker[Session],
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """Decision A6: real effects off (the default here: Lemlist mocked, no
    key), or a run asking `dry_run`: `{"skipped": true, "reason":
    "dry_run"}`, and NO call at all: no provider operation, no `GET /team`,
    no network attempt."""
    await _bind(db_session, email_world.agency_id)
    await _enable_job(db_session)
    violations = len(network_guard._VIOLATIONS)

    off = await _run(job_db)
    assert off.status == "success"
    assert off.stats == {"skipped": True, "reason": "dry_run"}
    assert "dry_run" in off.log_output

    with email_env(
        EMAIL_PROVIDER_EXTERNAL_EFFECTS="true", MOCK_LEMLIST="false", LEMLIST_API_KEY="k"
    ):
        asked = await _run(job_db, dry_run=True)
        assert asked.stats == {"skipped": True, "reason": "dry_run"}
        # The manual trigger passes `dry_run` through the API too — a
        # superadmin gesture (job.manage is platform-only).
        superadmin = await make_agent(role=system_roles["superadmin"])
        triggered = await client.post(
            "/jobs/email_sync/trigger", json={"dry_run": True}, headers=agent_headers(superadmin)
        )
        assert triggered.status_code == 200, triggered.text
        assert triggered.json()["stats"] == {"skipped": True, "reason": "dry_run"}
        # The platform's master switch off: skipped too, nothing asked.
        with email_env(EMAIL_MODULE_ENABLED="false"):
            master = await _run(job_db)
        assert master.stats == {"skipped": True, "reason": "module_disabled"}

    assert fake_email_provider.ops == []
    assert lemlist_team_double.requests == []
    assert len(network_guard._VIOLATIONS) == violations

    # Disabled in data (as seeded): the wrapper skips it before the pipeline.
    await db_session.execute(
        update(JobConfig).where(JobConfig.job_id == "email_sync").values(is_enabled=False)
    )
    await db_session.commit()
    disabled = await _run(job_db)
    assert disabled.status == "skipped" and "disabled" in disabled.log_output


# ── agencies: bound or skipped, one run per agency ───────────────────────────


async def test_only_the_agency_bound_to_the_keys_team_is_read(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
    make_agency: MakeAgency,
    make_agency_email_module: MakeAgencyEmailModule,
) -> None:
    """Several agencies have the module on; the key opens TEAM_A, bound to
    one of them (N4). The other agency (unbound, its own live campaign) and
    a third one bound to TEAM_B are skipped and counted; nothing of theirs
    is read. Prism's isolation (test_email_isolation L463), adapted."""
    ours, _ = await _live_campaign(client, make_client_profile, live.world, name="Ours")
    theirs, their_members = await _live_campaign(
        client,
        make_client_profile,
        live.world,
        name="Theirs",
        people=("Jean Dupont",),
        other_agency=True,
    )
    third = await make_agency(name="Third agency")
    await make_agency_email_module(agency_id=third.id, provider_team_id=TEAM_B)
    our_ref = await _provider_campaign(db_session, ours)
    their_ref = await _provider_campaign(db_session, theirs)
    jean = await _lead(db_session, their_members["Jean"])

    def campaigns_read() -> set[str]:
        return {kw["campaign"] for _, kw in live.provider.ops if "campaign" in kw}

    live.provider.ops.clear()
    run = await _run(live.sessions)
    assert run.status == "success", run.log_output
    assert (run.stats["agencies"], run.stats["agencies_skipped"]) == (1, 2)
    assert campaigns_read() == {our_ref}
    assert f"agency {live.world.other_agency_id}: skipped" in run.log_output
    assert f"agency {third.id}: skipped" in run.log_output

    # Even a provider answering with another agency's activity: an event of
    # their campaign, read while reconciling ours, is never stored.
    other_event = _event("j1", "OPENED", their_ref, jean.lead_id, 1)

    def answered(ref: CampaignRef, since: datetime | None) -> list[ProviderEvent]:
        live.provider.ops.append(("sync_campaign", {"campaign": ref.campaign_id}))
        return [other_event]

    live.provider.sync_campaign = answered  # type: ignore[method-assign]
    run = await _run(live.sessions)
    assert run.stats["new_events"] == 0
    stored = await db_session.scalars(select(EmailProviderEvent.provider_event_id))
    assert "j1" not in set(stored.all())

    # Asked directly for another agency's campaign, the reconciliation refuses.
    sync = EmailSyncManager(db_session, EmailManager(db_session))
    with pytest.raises(NotFoundError) as refused:
        await sync.reconcile_campaign(
            uuid.UUID(theirs),
            live.world.agency_id,
            provider=live.provider,
            key_team=TEAM_A,
        )
    assert refused.value.code == "email.campaign_not_found"


def _lock_sql(verb: str) -> Any:
    return text(f"SELECT {verb}(hashtextextended('email_sync:' || :agency, 0))")


async def test_a_busy_agency_is_skipped_and_its_lock_released_after_a_run(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
    sync_engine: Engine,
) -> None:
    await _live_campaign(client, make_client_profile, live.world, people=("Ana Lopez",))
    agency = {"agency": str(live.world.agency_id)}
    live.provider.ops.clear()
    with sync_engine.connect() as holder:
        assert holder.scalar(_lock_sql("pg_try_advisory_lock"), agency) is True
        holder.commit()
        busy = await _run(live.sessions)
        holder.scalar(_lock_sql("pg_advisory_unlock"), agency)
        holder.commit()
    assert busy.status == "success"
    assert (busy.stats["agencies"], busy.stats["agencies_skipped"]) == (0, 2)
    assert f"agency {live.world.agency_id}: skipped (busy" in busy.log_output
    assert live.provider.ops == []

    done = await _run(live.sessions)
    assert (done.stats["agencies"], done.stats["campaigns"]) == (1, 1)
    # Released at the end of the run: anyone may take it now.
    with sync_engine.connect() as after:
        assert after.scalar(_lock_sql("pg_try_advisory_lock"), agency) is True
        after.scalar(_lock_sql("pg_advisory_unlock"), agency)
        after.commit()


async def test_a_team_lemlist_does_not_name_reads_no_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
) -> None:
    """Decision A4: `GET /team` fails, so no agency is read in a team nobody
    named; the error is counted, as a code."""
    await _live_campaign(client, make_client_profile, live.world, people=("Ana Lopez",))
    live.provider.ops.clear()
    live.team.status_code = 503
    from src.email_campaigns import lemlist_team

    lemlist_team.clear_cache()
    run = await _run(live.sessions)
    assert run.status == "success"
    assert (run.stats["agencies"], run.stats["errors"]) == (0, 1)
    [error] = run.stats["error_messages"]
    assert error == {"code": "email.provider_unavailable", "reason": "team_unknown:http_503"}
    assert live.provider.ops == []


# ── one ingestion: each event stored once, whoever reads it ──────────────────


async def test_the_webhook_the_sync_and_the_job_store_an_event_once(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez",)
    )
    lemlist = await _provider_campaign(db_session, cid)
    ana = await _lead(db_session, members["Ana"])
    at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    payload = {
        "_id": "act_open",
        "type": "emailsOpened",
        "createdAt": at.isoformat(),
        "campaignId": lemlist,
        "leadId": ana.lead_id,
        "sequenceStep": 0,
        "secret": SECRET,
    }
    with _webhook_secret():
        first = (await client.post("/webhooks/lemlist", json=payload)).json()
    assert first == {"stored": True, "duplicate": False, "matched": True}
    live.provider.events = [_event("act_open", "OPENED", lemlist, ana.lead_id, 1, at=at)]

    synced = await sync_via_api(client, live.world.headers, cid)
    assert synced.status_code == 200, synced.text
    assert synced.json()["events_ingested"] == 0  # already stored by the webhook
    run = await _run(live.sessions)
    assert (run.stats["new_events"], run.stats["duplicate_events"]) == (0, 1)
    with _webhook_secret():
        again = (await client.post("/webhooks/lemlist", json=payload)).json()
    assert again == {"stored": False, "duplicate": True, "matched": False}

    count = await db_session.scalar(
        select(func.count())
        .select_from(EmailProviderEvent)
        .where(EmailProviderEvent.provider_event_id == "act_open")
    )
    assert count == 1
    assert (await _messages(db_session, ana.member_id))["out1"].open_count == 1


async def test_a_sync_reads_the_events_and_says_when_it_could_not(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
) -> None:
    """The manual Sync reads the campaign's events as Prism's does, through
    the same ingestion; a failing read is an error code, the sync goes on."""
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez",)
    )
    lemlist = await _provider_campaign(db_session, cid)
    ana = await _lead(db_session, members["Ana"])
    live.provider.events = [
        _event("s1", "SENT", lemlist, ana.lead_id, 1),
        # Another campaign's activity, even from the same provider: not ours.
        _event("x1", "SENT", "cam_not_ours", "lea_x", 1),
    ]
    synced = (await sync_via_api(client, live.world.headers, cid)).json()
    assert synced["events_ingested"] == 1 and synced["errors"] == []
    assert (await _member(db_session, ana.member_id)).status == "EMAIL_ACTIVE"

    live.provider.failures[("sync_campaign", lemlist)] = ProviderError(
        "Lemlist: server error", code="PROVIDER_ERROR", status=500
    )
    failed = (await sync_via_api(client, live.world.headers, cid)).json()
    assert failed["errors"] == [
        {"code": "events_not_read", "params": {"code": "email.provider_error"}}
    ]
    assert (await _campaign(db_session, cid)).sync_error == "events_not_read"


# ── NO HAND-OVER: the job never adds nor launches a lead (spec 8.5) ──────────


async def test_the_job_never_hands_a_lead_over_nor_writes_at_lemlist(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
    ai_calls: list[str],  # noqa: F811
) -> None:
    """Whatever the campaign holds (READY leads never launched, a lead added
    but refused by Lemlist's plan, running leads, a follow-up added and a
    template edited since), the job only reads: no lead added or launched,
    nothing pushed, paused or started, no AI value generated."""
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez",), ai=True
    )
    for first in ("Ben", "Cleo"):
        profile = await make_client_profile(
            agency_id=live.world.agency_id,
            first_name=first,
            last_name="Waiting",
            email=f"{first.lower()}@waiting.io",
        )
        await add_via_api(client, live.world.headers, cid, client_profile_ids=[profile.id])

    def refused(lead: LeadRef) -> None:
        raise ProviderError("Upgrade your plan", code="PLAN_REQUIRED", status=402)

    original = live.provider.launch_contact
    live.provider.launch_contact = refused  # type: ignore[method-assign]
    stopped = (await launch_via_api(client, live.world.headers, cid, limit=1)).json()
    live.provider.launch_contact = original  # type: ignore[method-assign]
    assert stopped["stopped"] is True
    await step_via_api(client, live.world.headers, cid, name="Encore", subject_template="")
    calls = list(ai_calls)

    live.provider.ops.clear()
    for _ in range(2):
        run = await _run(live.sessions)
        assert run.status == "success", run.log_output
    names = set(live.provider.names())
    assert names <= READS, names
    assert HAND_OVER.isdisjoint(names)
    assert ai_calls == calls
    statuses = {m["status"] for m in await _members(client, live.world.headers, cid)}
    assert statuses == {"EMAIL_QUEUED", "EMAIL_READY"}
    queued = await db_session.scalar(
        select(func.count())
        .select_from(EmailCampaignMember)
        .where(
            EmailCampaignMember.campaign_id == uuid.UUID(cid),
            EmailCampaignMember.queued_at.is_not(None),
        )
    )
    assert queued == 1  # Ana, through the Launch only
    assert members


async def test_the_boot_hands_nobody_over_and_never_schedules_the_disabled_job(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    sync_session_local: sessionmaker[Session],
    make_client_profile: MakeClientProfile,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The application's start-up (its lifespan: catalogue, bindings, seeds)
    and the scheduler built from the seeded jobs never reach the provider:
    READY leads wait for a Launch. The seeded `email_sync` row is disabled,
    so the scheduler does not even plan it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src import main

    cid = (await campaign_via_api(client, email_world.headers, "Waiting"))["id"]
    await step_via_api(client, email_world.headers, cid)
    profile = await make_client_profile(agency_id=email_world.agency_id, email="ana@wait.io")
    await add_via_api(client, email_world.headers, cid, client_profile_ids=[profile.id])
    await set_status(client, email_world.headers, cid, "ACTIVE")
    await seed_job_configs(db_session)

    monkeypatch.setattr(
        main, "async_session_maker", async_sessionmaker(bind=async_engine, expire_on_commit=False)
    )
    monkeypatch.setattr(main, "make_session_local", lambda: sync_session_local)
    async with main.lifespan(main.app):
        pass
    scheduler = build_scheduler(sync_session_local)
    scheduler.start(paused=True)
    try:
        assert scheduler.get_job("email_sync") is None
        assert scheduler.get_job("dispatch_reminders") is not None  # the others are
    finally:
        scheduler.shutdown(wait=False)
    assert fake_email_provider.ops == []
    # Read from the database: the boot's seeds published the legal documents,
    # which the agent would now have to accept before any API call.
    member = await db_session.scalar(
        select(EmailCampaignMember)
        .where(EmailCampaignMember.campaign_id == uuid.UUID(cid))
        .execution_options(populate_existing=True)
    )
    assert member is not None
    assert (member.status, member.provider_lead_id, member.queued_at) == (
        "EMAIL_READY",
        None,
        None,
    )


# ── the seed and the registry (spec 10.3) ────────────────────────────────────


async def test_the_job_is_seeded_disabled_once_and_registered(db_session: AsyncSession) -> None:
    [spec] = [c for c in DEFAULT_JOB_CONFIGS if c["job_id"] == "email_sync"]
    assert spec == {
        "job_id": "email_sync",
        "name": "Email Sync (Lemlist)",
        "cron_expression": "*/10 * * * *",
        "is_enabled": False,
    }
    assert "\u2014" not in spec["name"]
    await seed_job_configs(db_session)
    await seed_job_configs(db_session)  # every deployment: never a duplicate
    rows = (
        await db_session.scalars(select(JobConfig).where(JobConfig.job_id == "email_sync"))
    ).all()
    assert [(r.is_enabled, r.cron_expression, r.name) for r in rows] == [
        (False, "*/10 * * * *", "Email Sync (Lemlist)")
    ]
    assert JOB_REGISTRY["email_sync"] is sync_job.run_email_sync


# ── A1 and the content caps ──────────────────────────────────────────────────


async def test_an_unsubscribe_reaching_a_removed_lead_still_blocks_for_good(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
) -> None:
    """Decision A1: removed from the campaign, then Lemlist reports her
    unsubscribe: her membership takes the blocking status, keeps its
    `removed_at`, and no later reply lifts it; she is never added again."""
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez", "Ben Ready")
    )
    lemlist = await _provider_campaign(db_session, cid)
    ana, ben = await _lead(db_session, members["Ana"]), await _lead(db_session, members["Ben"])
    removed = await act(client, live.world.headers, "remove", [ana.member_id, ben.member_id])
    assert removed["failed"] == 0
    with _webhook_secret():
        for event_id, kind, lead in (
            ("u1", "emailsUnsubscribed", ana),
            ("r1", "emailsReplied", ana),
            ("b1", "emailsBounced", ben),
        ):
            answer = await client.post(
                "/webhooks/lemlist",
                json={
                    "_id": event_id,
                    "type": kind,
                    "campaignId": lemlist,
                    "leadId": lead.lead_id,
                    "sequenceStep": 0,
                    "secret": SECRET,
                },
            )
            assert answer.json() == {"stored": True, "duplicate": False, "matched": True}
    ana_row = await _member(db_session, ana.member_id)
    assert ana_row.status == "EMAIL_UNSUBSCRIBED" and ana_row.removed_at is not None
    assert ana_row.status_reason == "removed"
    ben_row = await _member(db_session, ben.member_id)
    assert ben_row.status == "EMAIL_BOUNCED" and ben_row.removed_at is not None

    other = (await campaign_via_api(client, live.world.headers, "Next"))["id"]
    profiles = [str(ana_row.client_profile_id), str(ben_row.client_profile_id)]
    plan = (
        await client.post(
            f"/email/campaigns/{other}/members/preview-add",
            json={"client_profile_ids": profiles},
            headers=live.world.headers,
        )
    ).json()
    assert {r["eligibility"] for r in plan["rows"]} == {"UNSUBSCRIBED", "BOUNCED"}


async def test_reply_contents_are_read_fifty_at_most_and_capped(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,
    make_client_profile: MakeClientProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prism's caps: a bounded number of reply contents per campaign and per
    run (50; 1 here), HTML kept to 500,000 characters, text to 50,000; a
    reply Lemlist attributes to a third party never matches."""
    monkeypatch.setattr(email_sync, "REPLIES_PER_RUN", 1)
    cid, members = await _live_campaign(
        client, make_client_profile, live.world, people=("Ana Lopez", "Ben Ready")
    )
    lemlist = await _provider_campaign(db_session, cid)
    ana, ben = await _lead(db_session, members["Ana"]), await _lead(db_session, members["Ben"])
    at = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    huge = "<p>" + "x" * 600_000 + "</p>"
    # The newest reply is read first: Ana's.
    live.provider.events = [
        _reply("rb", lemlist, ben, at),
        _reply("ra", lemlist, ana, at + timedelta(minutes=1)),
    ]
    assert ana.contact_id is not None and ben.contact_id is not None
    live.provider.inbox[ana.contact_id] = [
        _inbox_reply("ra", lemlist, ana, at + timedelta(minutes=1), huge)
    ]
    live.provider.inbox[ben.contact_id] = [
        # Another sender's reply in his thread, right then: never his.
        _inbox_reply("other", lemlist, ben, at, "<p>Not Ben</p>", isThirdPartyReply=True),
    ]
    first = await _run(live.sessions)
    assert first.stats["reply_contents"] == 1
    ana_reply = (await _messages(db_session, ana.member_id))["inra"]
    assert ana_reply.content_state == "AVAILABLE"
    assert ana_reply.body_html is not None and len(ana_reply.body_html) == 500_000
    assert ana_reply.body_text is not None and len(ana_reply.body_text) == 50_000
    # One per run: Ben's was not even read in this run.
    assert (await _messages(db_session, ben.member_id))["inrb"].content_attempts == 0
    second = await _run(live.sessions)
    assert second.stats["reply_contents"] == 0
    ben_reply = (await _messages(db_session, ben.member_id))["inrb"]
    assert (ben_reply.content_state, ben_reply.body_text) == ("PENDING", None)
    assert ben_reply.content_attempts == 1
