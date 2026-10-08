"""The import of the Lemlist campaigns already running in the agency's team
(lot C, decision of 08/10/2026): the plan (counts only), the application
(one transaction per campaign, idempotent), what an imported campaign still
takes (the guard), the rights, and the rule that the Sync, the job, the
webhook and the archive only READ an imported campaign at Lemlist, but for
the pause asked for.

Lemlist is `FakeProvider` (the manager's provider, scripted per campaign:
campaigns, sequences, listing, export, contacts lookup, activities), or the
real adapter on `FakeLemlist` where the HTTP methods are what is checked.
No test reaches Lemlist.
"""

import asyncio
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, selectinload, sessionmaker

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.client_profile import ClientProfile
from shared.models.email_campaign import (
    EmailCampaign,
    EmailCampaignMember,
    EmailMessage,
    EmailProviderEvent,
    EmailSuppression,
)
from shared.models.expat_user import ExpatUser
from shared.models.invitation import AgentInvitation, CaseInvitation
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from shared.models.usage import UsageEvent
from src.core import email as core_email
from src.core.rbac.permissions import Permission
from src.email_campaigns import email_manager, lemlist_team
from src.email_campaigns.email_import import CLIENT_PROFILE_CREATE, EmailImportManager
from src.email_campaigns.email_schema import EmailImportRequest
from src.email_campaigns.providers.base import (
    EmailProvider,
    ProviderCampaign,
    ProviderCampaignRow,
    ProviderContact,
    ProviderError,
    ProviderEvent,
    ProviderLead,
    ProviderLeadIdentity,
    ProviderSequences,
    ProviderSequenceStep,
)
from src.email_campaigns.suppression import email_hash
from tests.email_fakes import FakeLemlist, FakeProvider, lemlist_double
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    TEAM_A,
    TEAM_B,
    EmailWorld,
    LemlistTeamDouble,
    MakeCompanyLink,
    MakeEmailAiVariable,
    MakeEmailCampaign,
    MakeEmailMember,
    email_env,
)
from tests.plugins.rbac_plugin import MakeRole
from tests.test_email_campaigns import READ, act, campaign_via_api, role_headers, set_status
from tests.test_email_sync_engine import (  # noqa: F401 (fixtures)
    Live,
    _bind,
    _enable_job,
    _run,
    job_db,
    live,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

URL = "/email/import/lemlist"
SECRET = "s3cret-webhook"
#: FakeProvider's key team, unless a test binds the agency (TEAM_A).
TEAM = "tea_current"
LEMLIST_A = "cam_avocats_s2"
LEMLIST_B = "cam_avocats_s4"
#: What reading Lemlist may call on an imported campaign (the job, a Sync,
#: the webhook's pause re-read, the import's own reads).
READS = {
    "read_campaign",
    "list_contacts",
    "sync_campaign",
    "get_messages",
    "get_contact",
    "list_campaigns",
    "read_sequences",
    "export_leads",
    "lookup_contacts",
    "read_activities",
}
T0 = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


# ── scripting Lemlist ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Lead:
    """A Lemlist lead as a test scripts it: its listing (`state`, contact),
    its export row (identity, `status`), or none (`exported` False)."""

    lead_id: str
    email: str | None
    state: str | None
    status: str | None = "running"
    first: str | None = "Ana"
    last: str | None = "Lopez"
    company: str | None = None
    contact: str | None = None
    exported: bool = True


def lemlist_campaign(
    provider: FakeProvider,
    campaign_id: str,
    name: str,
    leads: Sequence[Lead],
    *,
    status: str = "paused",
    created_at: str | None = "2026-08-01T09:00:00.000Z",
    steps: Sequence[tuple[str, int]] = (("email", 0), ("email", 3)),
    branches: int = 0,
    ab_steps: Sequence[int] = (),
) -> None:
    sequence = f"seq_{campaign_id}"
    provider.campaign_rows.append(ProviderCampaignRow(campaign_id, name, status, created_at))
    provider.remote[campaign_id] = ProviderCampaign(
        campaign_id,
        exists=True,
        status=status,
        metadata={"status": status, "name": name, "timezone": "Europe/Lisbon"},
        sequence_id=sequence,
    )
    provider.sequence_reads[campaign_id] = ProviderSequences(
        sequence,
        [
            ProviderSequenceStep(
                step_id=f"stp_{campaign_id}_{index}",
                position=index,
                type=kind,
                delay_days=delay,
                ab_test=index in ab_steps,
                raw={
                    "_id": f"stp_{campaign_id}_{index}",
                    "type": kind,
                    "delay": delay,
                    "index": index,
                    "sequenceStep": index - 1,
                    "subject": "{{firstName}}, une question" if index == 1 else "",
                    "message": f"<p>Bonjour {{{{firstName}}}},</p><p>Message {index}</p>",
                },
            )
            for index, (kind, delay) in enumerate(steps, start=1)
        ],
        branches=branches,
        branch_steps=2 * branches,
    )
    provider.listed[campaign_id] = [
        ProviderLead(
            lead.lead_id,
            None,
            False,
            state=lead.state,
            contact_id=lead.contact,
            raw={"state": lead.state, **({"contactId": lead.contact} if lead.contact else {})},
        )
        for lead in leads
    ]
    provider.exports[campaign_id] = [
        ProviderLeadIdentity(
            lead.lead_id, lead.email, lead.first, lead.last, lead.company, lead.status, lead.state
        )
        for lead in leads
        if lead.exported
    ]


_TYPES = {
    "SENT": "emailsSent",
    "OPENED": "emailsOpened",
    "CLICKED": "emailsClicked",
    "REPLIED": "emailsReplied",
    "BOUNCED": "emailsBounced",
    "UNSUBSCRIBED": "entityUnsubscribed",
    "PAUSED": "paused",
}


def activity(
    event_id: str,
    kind: str,
    campaign: str,
    lead: str,
    step: int | None = 1,
    *,
    at: datetime = T0,
    **payload: Any,
) -> ProviderEvent:
    return ProviderEvent(
        event_id=event_id,
        type=kind,
        provider_type=_TYPES[kind],
        occurred_at=at,
        campaign_id=campaign,
        lead_id=lead,
        email=None,
        step_position=step,
        reason=None,
        payload={"_id": event_id, "campaignId": campaign, "leadId": lead, **payload},
    )


def a_leads() -> list[Lead]:
    """Campaign A (paused at Lemlist): one lead per rule of the mapping, and
    what the import sets aside."""
    return [
        Lead("lea_a01", "ana@acme.io", "emailsSent", company="Acme Avocats", contact="ctc_ana"),
        Lead("lea_a02", "ben@acme.io", "reviewed", first="Ben", last="Ready"),
        Lead("lea_a03", "cleo@acme.io", "emailsReplied", "done", "Cleo", "Doe", contact="ctc_cleo"),
        Lead("lea_a04", "dan@acme.io", "entityUnsubscribed", "unsubscribed", "Dan", "Moss"),
        Lead("lea_a05", "eve@acme.io", "emailsBounced", first="Eve", last="Stone"),
        Lead("lea_a06", "fred@acme.io", "emailsSent", "paused", "Fred", "Lake"),
        Lead("lea_a07", None, "emailsSent", first="Nobody", last="Here"),
        Lead("lea_a08", "gil@acme.io", "emailsSent", "done", "Gil", "Bow"),
        Lead("lea_a09", "hugo@acme.io", "skipped", first="Hugo", last="Vale"),
        Lead("lea_a10", "ivy@acme.io", "scanned", "review", "Ivy", "Wood"),
        Lead("lea_a11", "jo@acme.io", "emailsSent", "interested", "Jo", "March"),
        Lead("lea_a12", " ANA@acme.io ", "emailsOpened", first="Ana", last="Twice"),
        Lead("lea_a13", None, "emailsClicked", contact="ctc_kim", exported=False),
        Lead(
            "lea_a14", "lea@acme.io", "emailsSent", first="Lea", last="Known", company="Lemlist Co"
        ),
        Lead("lea_a15", "max@acme.io", "emailsBounced", first="Max", last="Elsewhere"),
        Lead("lea_a16", "nina@acme.io", "reviewed", first=None, last=None),
    ]


def a_history() -> list[ProviderEvent]:
    return [
        activity("act_a01_s", "SENT", LEMLIST_A, "lea_a01", contactId="ctc_ana"),
        activity("act_a01_o", "OPENED", LEMLIST_A, "lea_a01", at=T0 + timedelta(hours=1)),
        # A past reply of a lead Lemlist still runs: kept, the status is not moved.
        activity("act_a01_r", "REPLIED", LEMLIST_A, "lea_a01", at=T0 + timedelta(hours=2)),
        # A conditional branch's step (not imported): no step for it.
        activity(
            "act_a01_branch",
            "SENT",
            LEMLIST_A,
            "lea_a01",
            1,
            at=T0 + timedelta(hours=3),
            sequenceId="seq_branch",
        ),
        activity("act_a03_s", "SENT", LEMLIST_A, "lea_a03", at=T0 - timedelta(days=2)),
        activity(
            "act_a03_r",
            "REPLIED",
            LEMLIST_A,
            "lea_a03",
            at=T0 - timedelta(days=1),
            contactId="ctc_cleo",
        ),
        activity("act_a04_u", "UNSUBSCRIBED", LEMLIST_A, "lea_a04", None, contactId="ctc_dan"),
        activity("act_a05_b", "BOUNCED", LEMLIST_A, "lea_a05"),
        # A past bounce of a lead Lemlist now has paused: listed, still paused.
        activity("act_a06_b", "BOUNCED", LEMLIST_A, "lea_a06"),
        activity("act_a08_s1", "SENT", LEMLIST_A, "lea_a08", at=T0 - timedelta(days=3)),
        # Lemlist names the step: it wins over a rank that moved.
        activity("act_a08_s2", "SENT", LEMLIST_A, "lea_a08", 6, stepId=f"stp_{LEMLIST_A}_2"),
    ]


def b_leads() -> list[Lead]:
    """Campaign B (running, newer): Ana again (live in A: a conflict in the
    plan), a new person, Cleo again (replied in A, not live there)."""
    return [
        Lead("lea_b01", "ana@acme.io", "emailsSent"),
        Lead("lea_b02", "noe@acme.io", "reviewed", first="Noe", last="Fresh"),
        Lead("lea_b03", "cleo@acme.io", "emailsSent", first="Cleo", last="Doe"),
    ]


@dataclass
class Team:
    """The agency's facts the scenario needs, as plain ids."""

    world: EmailWorld
    provider: FakeProvider
    lea_profile: uuid.UUID
    max_profile: uuid.UUID
    nidria_campaign: uuid.UUID
    nidria_member: uuid.UUID


async def team_scenario(
    world: EmailWorld,
    provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> Team:
    """The team at Lemlist: A (paused) and B (running) importable, a draft,
    an ended, an archived and an erroneous campaign, and one Nidria made.
    In Nidria: Lea's record (linked to a company), and Max live in a
    campaign Nidria made."""
    lea = await make_client_profile(
        agency_id=world.agency_id, first_name="Lea", last_name="Known", email="lea@acme.io"
    )
    await make_company_link(client_profile_id=lea.id, agency_id=world.agency_id, name="Lea SARL")
    max_ = await make_client_profile(
        agency_id=world.agency_id, first_name="Max", last_name="Elsewhere", email="max@acme.io"
    )
    own = await make_email_campaign(agency_id=world.agency_id, name="Nidria own")
    member = await make_email_member(campaign_id=own.id, client_profile_id=max_.id)
    nidria = await make_email_campaign(
        agency_id=world.agency_id,
        name="Launched from Nidria",
        status="ACTIVE",
        provider_campaign_id="cam_nidria",
        provider_metadata={"team_id": provider.team},
    )
    lemlist_campaign(provider, LEMLIST_A, "2026-08 · Avocats · s2", a_leads())
    provider.contacts["ctc_kim"] = ProviderContact(
        "ctc_kim",
        "Kim@Acme.io",
        "Kim",
        "Park",
        lead_statuses={(LEMLIST_A, "lea_a13"): "running"},
    )
    lemlist_campaign(
        provider,
        LEMLIST_B,
        "2026-09 · Avocats · s4",
        b_leads(),
        status="running",
        created_at="2026-09-01T09:00:00.000Z",
        steps=(("email", 0), ("linkedinSend", 2), ("email", 3)),
        branches=1,
        ab_steps=(1,),
    )
    for campaign_id, name, status, created in (
        ("cam_draft", "s6 EN · BROUILLON", "draft", "2026-10-01T09:00:00.000Z"),
        ("cam_ended", "2026-08 · Test", "ended", "2026-07-01T09:00:00.000Z"),
        ("cam_archived", "Old", "archived", "2026-06-01T09:00:00.000Z"),
        ("cam_errors", "Broken", "errors", "2026-05-01T09:00:00.000Z"),
        ("cam_nidria", "Launched from Nidria", "running", "2026-09-15T09:00:00.000Z"),
    ):
        provider.campaign_rows.append(ProviderCampaignRow(campaign_id, name, status, created))
    provider.events = a_history()
    return Team(world, provider, lea.id, max_.id, nidria.id, member.id)


@pytest_asyncio.fixture
async def team(
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_company_link: MakeCompanyLink,
) -> Team:
    return await team_scenario(
        email_world,
        fake_email_provider,
        make_client_profile,
        make_email_campaign,
        make_email_member,
        make_company_link,
    )


async def plan_of(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get(URL, headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def import_(client: AsyncClient, headers: dict[str, str], *ids: str) -> dict[str, Any]:
    response = await client.post(URL, json={"campaign_ids": list(ids)}, headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["provider_campaign_id"]: row for row in result["campaigns"]}


async def campaign_of(db: AsyncSession, provider_campaign_id: str) -> EmailCampaign:
    found = await db.scalar(
        select(EmailCampaign)
        .where(EmailCampaign.provider_campaign_id == provider_campaign_id)
        .options(selectinload(EmailCampaign.steps))
        .execution_options(populate_existing=True)
    )
    assert found is not None
    return found


async def members_by_lead(
    db: AsyncSession, campaign_id: uuid.UUID
) -> dict[str, EmailCampaignMember]:
    rows = await db.scalars(
        select(EmailCampaignMember)
        .where(EmailCampaignMember.campaign_id == campaign_id)
        .execution_options(populate_existing=True)
    )
    return {str(m.provider_lead_id): m for m in rows.all()}


async def count(db: AsyncSession, model: Any, *where: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


# ── the plan ─────────────────────────────────────────────────────────────────


async def test_the_plan_counts_what_an_import_would_do_campaign_by_campaign(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    provider = team.provider
    plan = await plan_of(client, team.world.headers)

    # Importable: running or paused, oldest first; the others apart.
    assert [c["provider_campaign_id"] for c in plan["campaigns"]] == [LEMLIST_A, LEMLIST_B]
    assert [(c["provider_campaign_id"], c["status"]) for c in plan["not_importable"]] == [
        ("cam_errors", "errors"),
        ("cam_archived", "archived"),
        ("cam_ended", "ended"),
        ("cam_draft", "draft"),
    ]
    assert [c["provider_campaign_id"] for c in plan["created_by_nidria"]] == ["cam_nidria"]
    assert plan["listing_complete"] is True

    a, b = plan["campaigns"]
    assert (a["name"], a["status"], a["already_imported"], a["error"]) == (
        "2026-08 · Avocats · s2",
        "paused",
        False,
        None,
    )
    assert a["steps"] == {
        "by_type": {"email": 2},
        "imported": 2,
        "branches": 0,
        "branch_steps": 0,
        "ab_tests": 0,
    }
    assert b["steps"] == {
        "by_type": {"email": 2, "linkedinSend": 1},
        "imported": 3,
        "branches": 1,
        "branch_steps": 2,
        "ab_tests": 1,
    }
    assert a["leads"] == {
        "listed": 16,
        "listing_complete": True,
        "listing_duplicates": 0,
        "identities": 16,
        "from_export": 15,
        "from_contacts": 1,
        "without_email": 1,
        "existing_profiles": 1,
        "profiles_to_create": 9,
        "profiles_planned_before": 0,
        "conflicts": 1,
        "conflicts_in_plan": 0,
        "duplicate_emails": 1,
        "to_create": 10,
        "to_update": 0,
        "not_imported": {
            "conflict": 1,
            "duplicate_email": 1,
            "no_email": 1,
            "not_launched": 1,
            "skipped_at_provider": 1,
            "status_uncovered": 1,
        },
    }
    # B: Ana is live in A (planned before), Cleo's record comes with A.
    assert (
        b["leads"]["conflicts_in_plan"],
        b["leads"]["profiles_to_create"],
        b["leads"]["profiles_planned_before"],
        b["leads"]["to_create"],
    ) == (1, 1, 1, 2)

    states = {(s["state"], s["status"]): s for s in a["states"]}
    assert states[("emailsSent", "running")]["count"] == 3
    assert states[("emailsSent", "running")]["proposed"] == "EMAIL_ACTIVE"
    assert states[("reviewed", "running")]["proposed"] == "EMAIL_QUEUED"
    assert states[("entityUnsubscribed", "unsubscribed")]["opposition"] == "UNSUBSCRIBED"
    assert states[("emailsBounced", "running")]["opposition"] == "BOUNCED"
    assert states[("emailsSent", "interested")]["rule"] == "uncovered"
    assert states[("emailsSent", "interested")]["proposed"] is None
    assert states[("emailsClicked", "running")]["proposed"] == "EMAIL_ACTIVE"

    # Calls: campaign, sequences, one listing page, the export, one lookup.
    assert a["calls"] == {"reads": 5, "apply_min": 6}
    assert b["calls"] == {"reads": 4, "apply_min": 5}
    assert plan["calls"] == {"listing_pages": 1, "reads": 9, "total": 10, "apply_min": 12}
    assert [rule["rule"] for rule in plan["rules"]][:3] == ["unsubscribed", "bounced", "replied"]

    # Reads only, at Lemlist and in Nidria.
    assert set(provider.names()) <= READS and "read_activities" not in provider.names()
    assert await count(db_session, EmailCampaign) == 2
    assert await count(db_session, ClientProfile) == 2


async def test_the_plan_names_no_address_and_no_person(client: AsyncClient, team: Team) -> None:
    response = await client.get(URL, headers=team.world.headers)
    text = response.text
    assert response.status_code == 200
    assert "@" not in text
    for lead in [*a_leads(), *b_leads()]:
        for value in (lead.first, lead.last, lead.company):
            if value:
                assert value not in text, value
    assert "Park" not in text and "Kim" not in text


# ── the application ──────────────────────────────────────────────────────────


async def test_an_import_mirrors_the_campaign_its_people_and_its_history(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    world, provider = team.world, team.provider
    result = await import_(client, world.headers, LEMLIST_A)
    [row] = result["campaigns"]
    assert row["ok"] is True and row["created"] is True
    assert (row["members_created"], row["members_updated"], row["profiles_created"]) == (10, 0, 9)
    assert (row["steps_created"], row["steps_updated"]) == (2, 0)
    assert row["conflicts"] == 1
    assert row["not_imported"] == {
        "conflict": 1,
        "duplicate_email": 1,
        "no_email": 1,
        "not_launched": 1,
        "skipped_at_provider": 1,
        "status_uncovered": 1,
    }
    assert (row["events_stored"], row["events_duplicate"]) == (11, 0)
    assert (row["activities_read"], row["activity_pages"], row["activities_complete"]) == (
        11,
        1,
        True,
    )
    assert row["replies_pending"] == 2 and row["unknown_leads"] == 0
    # Dan (unsubscribed), Eve (bounced), and Max (bounced, set aside).
    assert row["opposition_listed"] == 3
    assert result["calls"] == 1 + 9 + 1

    campaign = await campaign_of(db_session, LEMLIST_A)
    assert (campaign.origin, campaign.status, campaign.provider_status) == (
        "LEMLIST_IMPORT",
        "PAUSED",
        "paused",
    )
    assert campaign.name == "2026-08 · Avocats · s2"
    assert campaign.provider_sequence_id == f"seq_{LEMLIST_A}"
    assert campaign.provider_metadata["team_id"] == TEAM
    assert campaign.settings["timezone"] == "Europe/Lisbon"
    assert campaign.last_activity_sync_at is not None
    noted = campaign.provider_metadata["import"]
    assert noted["unknown_leads"] == 0 and len(noted["set_aside"]) == 6
    steps = sorted(campaign.steps, key=lambda s: s.position)
    assert [(s.type, s.delay_value, s.name, s.subject_template) for s in steps] == [
        ("EMAIL", 0, "", ""),
        ("EMAIL", 3, "", ""),
    ]
    assert steps[0].provider_content is not None
    assert steps[0].provider_content["subject"] == "{{firstName}}, une question"

    members = await members_by_lead(db_session, campaign.id)
    assert {lead: m.status for lead, m in members.items()} == {
        "lea_a01": "EMAIL_ACTIVE",
        "lea_a02": "EMAIL_QUEUED",
        "lea_a03": "EMAIL_REPLIED",
        "lea_a04": "EMAIL_UNSUBSCRIBED",
        "lea_a05": "EMAIL_BOUNCED",
        "lea_a06": "EMAIL_PAUSED",
        "lea_a08": "EMAIL_COMPLETED",
        "lea_a13": "EMAIL_ACTIVE",
        "lea_a14": "EMAIL_ACTIVE",
        "lea_a16": "EMAIL_QUEUED",
    }
    ana = members["lea_a01"]
    # The history fills the record and never moves the status Lemlist gives.
    assert ana.status == "EMAIL_ACTIVE" and ana.last_replied_at is not None
    assert (ana.current_step, ana.started_at, ana.queued_at) == (1, T0, T0)
    assert (ana.provider_status, ana.provider_contact_id) == ("emailsSent", "ctc_ana")
    assert ana.provider_metadata["import"]["company_name"] == "Acme Avocats"
    assert members["lea_a08"].current_step == 2
    assert members["lea_a06"].status_reason == "paused_at_provider"
    assert members["lea_a04"].status_reason == "unsubscribed_at_provider"
    assert members["lea_a14"].client_profile_id == team.lea_profile

    # Records: created by the import, as Lemlist names them, nothing else.
    created = (
        await db_session.scalars(
            select(ClientProfile)
            .where(ClientProfile.source == "lemlist")
            .execution_options(populate_existing=True)
        )
    ).all()
    assert len(created) == 9
    names = {p.email: (p.first_name, p.last_name, p.expat_user_id) for p in created}
    assert names["ana@acme.io"] == ("Ana", "Lopez", None)
    assert names["kim@acme.io"] == ("Kim", "Park", None)
    assert names["nina@acme.io"] == (None, None, None)
    assert core_email.outbox == []
    for model in (AgentInvitation, CaseInvitation, ExpatUser, ActivityLog, UsageEvent, Reminder):
        assert await count(db_session, model) == 0, model

    # Messages: sent by Lemlist from its own template, replies to read.
    messages = (
        await db_session.scalars(
            select(EmailMessage)
            .where(EmailMessage.campaign_id == campaign.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    outbound = [m for m in messages if m.direction == "OUTBOUND"]
    assert {m.content_state for m in outbound} == {"PROVIDER_ONLY"}
    assert len(outbound) == 6  # Ana 1, Cleo 1, Eve 1, Fred 1, Gil 2; the branch's none
    assert sorted(m.content_state for m in messages if m.direction == "INBOUND") == [
        "PENDING",
        "PENDING",
    ]
    branch = await db_session.scalar(
        select(EmailProviderEvent).where(EmailProviderEvent.provider_event_id == "act_a01_branch")
    )
    assert branch is not None and branch.step_position is None

    # The opposition list: imported blocks, a past bounce, a conflict's bounce.
    listed = set(
        (
            await db_session.scalars(
                select(EmailSuppression.email_hash).where(
                    EmailSuppression.agency_id == world.agency_id
                )
            )
        ).all()
    )
    assert listed == {
        email_hash(a) for a in ("dan@acme.io", "eve@acme.io", "fred@acme.io", "max@acme.io")
    }
    # Max is never imported: live in Nidria's own campaign.
    max_member = await db_session.get(EmailCampaignMember, team.nidria_member)
    assert max_member is not None and max_member.status == "EMAIL_READY"
    # Lemlist was only read.
    assert set(provider.names()) <= READS


async def test_an_imported_campaign_reads_truthfully(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    headers = team.world.headers
    await import_(client, headers, LEMLIST_A, LEMLIST_B)
    a = await campaign_of(db_session, LEMLIST_A)
    b = await campaign_of(db_session, LEMLIST_B)
    detail = (await client.get(f"/email/campaigns/{b.id}", headers=headers)).json()
    assert (detail["origin"], detail["status"], detail["unknown_leads"]) == (
        "LEMLIST_IMPORT",
        "ACTIVE",
        0,
    )
    assert detail["last_imported_at"] is not None and detail["provider_mode"] == "LIVE"
    assert detail["sequence"]["can_add_step"] is False
    assert set(detail["sequence"]["reasons"].values()) == {"imported"}
    first, linkedin, _third = detail["steps"]
    assert first["capabilities"]["delete_reason"] == "imported"
    assert (first["type"], first["provider_type"], first["raw_subject"]) == (
        "EMAIL",
        "email",
        "{{firstName}}, une question",
    )
    assert first["raw_body_text"] == "Bonjour {{firstName}},\n\nMessage 1"
    assert "<p>" not in str(detail)
    assert (linkedin["type"], linkedin["provider_type"]) == ("OTHER_CHANNEL", "linkedinSend")
    assert (first["subject_template"], first["body_template"], first["unsynced"]) == ("", "", False)
    campaigns = (await client.get("/email/campaigns", headers=headers)).json()
    origins = {c["name"]: c["origin"] for c in campaigns}
    assert (
        origins["Nidria own"] == "NIDRIA" and origins["2026-09 · Avocats · s4"] == "LEMLIST_IMPORT"
    )

    members = await members_by_lead(db_session, a.id)
    listing = (
        await client.get(
            "/email/members", params={"campaign_id": str(a.id), "page_size": 50}, headers=headers
        )
    ).json()
    rows = {row["id"]: row for row in listing["items"]}
    ana = rows[str(members["lea_a01"].id)]
    # Lemlist's company, the record having none: shown, said so.
    assert (ana["company_name"], ana["company_id"], ana["company_from_provider"]) == (
        "Acme Avocats",
        None,
        True,
    )
    # A record linked to a company of the agency keeps it.
    lea = rows[str(members["lea_a14"].id)]
    assert (lea["company_name"], lea["company_from_provider"]) == ("Lea SARL", False)

    drawer = (await client.get(f"/email/members/{members['lea_a01'].id}", headers=headers)).json()
    step_one = drawer["steps"][0]
    # Sent by Lemlist from its template: no text claimed as the email sent.
    assert (step_one["content_state"], step_one["subject"], step_one["body"]) == (
        "PROVIDER_ONLY",
        None,
        None,
    )
    # Her past reply answered step 1: its furthest state.
    assert step_one["status"] == "REPLIED" and step_one["open_count"] == 1
    sent = [c for c in drawer["conversation"] if c["direction"] == "OUTBOUND"]
    assert sent and {c["content_state"] for c in sent} == {"PROVIDER_ONLY"}
    # A running lead of an imported campaign is never dated by Nidria.
    b_members = await members_by_lead(db_session, b.id)
    queued = (await client.get(f"/email/members/{b_members['lea_b02'].id}", headers=headers)).json()
    assert queued["schedule_hold"] == {"code": "provider_schedule", "params": {}}
    assert all(step["planned_on"] is None for step in queued["steps"])


async def test_the_import_follows_the_plan_and_answers_campaign_by_campaign(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    result = await import_(
        client,
        team.world.headers,
        LEMLIST_B,
        "cam_unknown",
        "cam_draft",
        "cam_nidria",
        LEMLIST_A,
    )
    # The plan's order (A before B), then the refused, as asked.
    assert [r["provider_campaign_id"] for r in result["campaigns"]] == [
        LEMLIST_A,
        LEMLIST_B,
        "cam_unknown",
        "cam_draft",
        "cam_nidria",
    ]
    rows = by_id(result)
    assert rows["cam_unknown"]["code"] == "email.import_campaign_not_found"
    assert (rows["cam_draft"]["code"], rows["cam_draft"]["params"]) == (
        "email.import_not_importable",
        {"status": "draft"},
    )
    assert rows["cam_nidria"]["code"] == "email.import_created_by_nidria"
    assert not any(rows[i]["ok"] for i in ("cam_unknown", "cam_draft", "cam_nidria"))
    b = rows[LEMLIST_B]
    # Ana is live in A by now: a conflict; Cleo's record exists (made by A).
    assert b["ok"] is True and b["not_imported"] == {"conflict": 1}
    assert (b["members_created"], b["profiles_created"]) == (2, 1)
    nidria = await db_session.get(EmailCampaign, team.nidria_campaign)
    assert nidria is not None and nidria.origin == "NIDRIA"
    # Never two campaigns for one Lemlist campaign.
    assert (
        await count(db_session, EmailCampaign, EmailCampaign.provider_campaign_id == "cam_nidria")
        == 1
    )
    cleo = await count(db_session, ClientProfile, ClientProfile.email == "cleo@acme.io")
    assert cleo == 1


async def test_a_campaign_lemlist_ended_or_lost_since_its_listing_is_not_imported(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    provider = team.provider
    provider.remote[LEMLIST_A] = ProviderCampaign(
        LEMLIST_A, exists=True, status="ended", sequence_id=f"seq_{LEMLIST_A}"
    )
    provider.remote[LEMLIST_B] = ProviderCampaign(LEMLIST_B, exists=False)
    plan = await plan_of(client, team.world.headers)
    a, b = plan["campaigns"]
    assert (a["error"], a["status"], a["leads"], a["steps"]) == (
        "email.import_not_importable",
        "ended",
        None,
        None,
    )
    assert b["error"] == "email.provider_campaign_missing"
    rows = by_id(await import_(client, team.world.headers, LEMLIST_A, LEMLIST_B))
    assert (rows[LEMLIST_A]["ok"], rows[LEMLIST_A]["code"], rows[LEMLIST_A]["params"]) == (
        False,
        "email.import_not_importable",
        {"status": "ended"},
    )
    assert rows[LEMLIST_B]["code"] == "email.provider_campaign_missing"
    assert await count(db_session, EmailCampaign, EmailCampaign.origin == "LEMLIST_IMPORT") == 0
    assert "read_activities" not in team.provider.names()


async def test_a_failed_campaign_is_undone_alone(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Team,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = team.provider
    provider.failures[("read_activities", LEMLIST_A)] = ProviderError(
        "Lemlist could not be reached.", code="UNREACHABLE"
    )
    rows = by_id(await import_(client, team.world.headers, LEMLIST_A, LEMLIST_B))
    assert (rows[LEMLIST_A]["ok"], rows[LEMLIST_A]["code"]) == (
        False,
        "email.provider_unreachable",
    )
    assert rows[LEMLIST_A]["members_created"] == 0
    assert rows[LEMLIST_B]["ok"] is True
    # Ana is a conflict of the plan (A planned her live) even though A failed.
    assert rows[LEMLIST_B]["not_imported"] == {"conflict_in_plan": 1}
    assert (
        await count(db_session, EmailCampaign, EmailCampaign.provider_campaign_id == LEMLIST_A) == 0
    )

    # A failure inside the transaction, once records, memberships and
    # history are written: nothing of that campaign is kept, and the log
    # names the campaign and the error's class, never what it carries.
    del provider.failures[("read_activities", LEMLIST_A)]

    async def broken(self: EmailImportManager, *_args: Any) -> None:
        raise RuntimeError("duplicate key (email)=(ana@acme.io) Ana Lopez")

    monkeypatch.setattr(EmailImportManager, "_opposition", broken)
    profiles = await count(db_session, ClientProfile)
    events = await count(db_session, EmailProviderEvent)
    with caplog.at_level("ERROR", logger="src.email_campaigns.email_import"):
        rows = by_id(await import_(client, team.world.headers, LEMLIST_A))
    assert (rows[LEMLIST_A]["ok"], rows[LEMLIST_A]["code"]) == (False, "email.import_failed")
    assert (
        await count(db_session, EmailCampaign, EmailCampaign.provider_campaign_id == LEMLIST_A) == 0
    )
    assert await count(db_session, ClientProfile) == profiles
    assert await count(db_session, EmailProviderEvent) == events
    logged = caplog.text
    assert LEMLIST_A in logged and "RuntimeError" in logged
    assert "@" not in logged and "Lopez" not in logged


async def test_importing_again_refreshes_without_a_duplicate_and_never_lifts_a_block(
    client: AsyncClient, db_session: AsyncSession, team: Team
) -> None:
    world, provider = team.world, team.provider
    await import_(client, world.headers, LEMLIST_A)
    campaign = await campaign_of(db_session, LEMLIST_A)
    before = {
        model: await count(db_session, model)
        for model in (EmailCampaign, ClientProfile, EmailMessage, EmailProviderEvent)
    }

    # Since then at Lemlist: Ben contacted, Fred resumed, Dan running again
    # (a block is never lifted), a new lead, a step's delay changed.
    leads = [
        Lead("lea_a02", "ben@acme.io", "emailsSent", first="Ben", last="Ready")
        if lead.lead_id == "lea_a02"
        else Lead("lea_a06", "fred@acme.io", "emailsSent", first="Fred", last="Lake")
        if lead.lead_id == "lea_a06"
        else Lead("lea_a04", "dan@acme.io", "emailsSent", first="Dan", last="Moss")
        if lead.lead_id == "lea_a04"
        else lead
        for lead in a_leads()
    ] + [Lead("lea_a17", "omar@acme.io", "emailsSent", first="Omar", last="New")]
    provider.campaign_rows = [r for r in provider.campaign_rows if r.campaign_id != LEMLIST_A]
    lemlist_campaign(
        provider,
        LEMLIST_A,
        "2026-08 · Avocats · s2 (bis)",
        leads,
        steps=(("email", 0), ("email", 5)),
    )
    provider.events = [*a_history(), activity("act_a17_s", "SENT", LEMLIST_A, "lea_a17")]

    [row] = (await import_(client, world.headers, LEMLIST_A))["campaigns"]
    assert row["ok"] is True and row["created"] is False
    assert (row["members_created"], row["members_updated"], row["profiles_created"]) == (1, 10, 1)
    assert (row["steps_created"], row["steps_updated"]) == (0, 1)
    assert (row["events_stored"], row["events_duplicate"]) == (1, 11)

    after = {
        model: await count(db_session, model)
        for model in (EmailCampaign, ClientProfile, EmailMessage, EmailProviderEvent)
    }
    assert after[EmailCampaign] == before[EmailCampaign]
    assert after[ClientProfile] == before[ClientProfile] + 1
    assert after[EmailProviderEvent] == before[EmailProviderEvent] + 1
    assert after[EmailMessage] == before[EmailMessage] + 1
    refreshed = await campaign_of(db_session, LEMLIST_A)
    assert refreshed.id == campaign.id and refreshed.name == "2026-08 · Avocats · s2 (bis)"
    assert [s.delay_value for s in sorted(refreshed.steps, key=lambda s: s.position)] == [0, 5]
    members = await members_by_lead(db_session, campaign.id)
    assert members["lea_a02"].status == "EMAIL_ACTIVE"
    assert (members["lea_a06"].status, members["lea_a06"].paused_at) == ("EMAIL_ACTIVE", None)
    assert members["lea_a04"].status == "EMAIL_UNSUBSCRIBED"
    assert members["lea_a17"].status == "EMAIL_ACTIVE"
    assert len(members) == 11


async def test_two_imports_at_once_make_one_campaign_and_one_membership_per_lead(
    db_session: AsyncSession, async_engine: AsyncEngine, team: Team
) -> None:
    """The agency's import lock and the unique campaign index: two
    applications racing write everything once."""
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    async def apply() -> Any:
        async with sessions() as session:
            agent = await session.scalar(
                select(Agent)
                .where(Agent.id == team.world.admin_id)
                .options(selectinload(Agent.role).selectinload(Role.permissions))
            )
            assert agent is not None
            return await EmailImportManager(session).apply(
                agent, EmailImportRequest(campaign_ids=[LEMLIST_A])
            )

    first, second = await asyncio.gather(apply(), apply())
    created = sorted(r.created for r in (*first.campaigns, *second.campaigns))
    assert created == [False, True]
    assert all(r.ok for r in (*first.campaigns, *second.campaigns))
    assert await count(db_session, EmailCampaign, EmailCampaign.origin == "LEMLIST_IMPORT") == 1
    campaign = await campaign_of(db_session, LEMLIST_A)
    rows = (
        await db_session.execute(
            select(EmailCampaignMember.provider_lead_id, func.count())
            .where(EmailCampaignMember.campaign_id == campaign.id)
            .group_by(EmailCampaignMember.provider_lead_id)
        )
    ).all()
    assert len(rows) == 10 and {n for _lead, n in rows} == {1}
    assert await count(db_session, ClientProfile, ClientProfile.source == "lemlist") == 9


async def test_hundreds_of_leads_import_quickly(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
) -> None:
    """No query per lead: a few hundred leads cost a constant number of
    statements (their records and memberships are read and written in
    batches); their history costs its one ingestion per event (the event,
    its membership, its message), and the whole stays quick."""
    leads = [
        Lead(f"lea_{i:04d}", f"person{i}@example.org", "emailsSent", first=f"P{i}", last="Bulk")
        for i in range(300)
    ]
    statements = 0

    def counted(*_args: Any) -> None:
        nonlocal statements
        statements += 1

    engine = db_session.bind
    assert isinstance(engine, AsyncEngine)

    async def imported_with(campaign_id: str) -> tuple[dict[str, Any], int, float]:
        nonlocal statements
        statements = 0
        event.listen(engine.sync_engine, "before_cursor_execute", counted)
        started = time.perf_counter()
        try:
            [row] = (await import_(client, email_world.headers, campaign_id))["campaigns"]
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", counted)
        return row, statements, time.perf_counter() - started

    lemlist_campaign(fake_email_provider, LEMLIST_A, "Bulk", leads, status="running")
    row, quiet, _ = await imported_with(LEMLIST_A)
    assert row["members_created"] == 300 and row["profiles_created"] == 300
    assert quiet < 80, quiet  # whatever the number of leads

    fake_email_provider.campaign_rows.clear()
    more = [Lead(f"lea_b{i:04d}", f"other{i}@example.org", "emailsSent") for i in range(300)]
    lemlist_campaign(fake_email_provider, LEMLIST_B, "Bulk with history", more, status="running")
    fake_email_provider.events = [
        activity(f"act_{i}_{kind}", kind, LEMLIST_B, f"lea_b{i:04d}")
        for i in range(300)
        for kind in ("SENT", "OPENED")
    ]
    row, busy, elapsed = await imported_with(LEMLIST_B)
    assert row["members_created"] == 300 and row["events_stored"] == 600
    assert busy < quiet + 6 * 600, busy
    assert elapsed < 60, elapsed


# ── the guard: what an imported campaign still takes ─────────────────────────


@dataclass
class Imported:
    campaign_id: str
    step_id: str
    ai_variable_id: str
    members: dict[str, str]
    nidria_campaign_id: str


@pytest_asyncio.fixture
async def imported(
    client: AsyncClient,
    db_session: AsyncSession,
    team: Team,
    make_email_ai_variable: MakeEmailAiVariable,
) -> Imported:
    await import_(client, team.world.headers, LEMLIST_A)
    campaign = await campaign_of(db_session, LEMLIST_A)
    step = min(campaign.steps, key=lambda s: s.position)
    # An AI variable on an imported step (only a database row could make one).
    variable = await make_email_ai_variable(campaign_step_id=step.id)
    members = await members_by_lead(db_session, campaign.id)
    nidria = await campaign_via_api(client, team.world.headers, "Nidria target")
    return Imported(
        campaign_id=str(campaign.id),
        step_id=str(step.id),
        ai_variable_id=str(variable.id),
        members={lead: str(m.id) for lead, m in members.items()},
        nidria_campaign_id=nidria["id"],
    )


async def test_every_write_nidria_would_make_is_refused(
    client: AsyncClient, db_session: AsyncSession, team: Team, imported: Imported
) -> None:
    headers = team.world.headers
    cid, step, variable = imported.campaign_id, imported.step_id, imported.ai_variable_id
    campaign = await campaign_of(db_session, LEMLIST_A)
    step_ids = [str(s.id) for s in sorted(campaign.steps, key=lambda s: s.position)]
    team.provider.ops.clear()
    refused = [
        ("PATCH", f"/email/campaigns/{cid}", {"name": "Renamed"}),
        ("PATCH", f"/email/campaigns/{cid}", {"settings": {"daily_limit": 10}}),
        (
            "PATCH",
            f"/email/campaigns/{cid}",
            {"settings": {"sender": {"user_id": "usr_1", "email": "sender@nidria.test"}}},
        ),
        ("POST", f"/email/campaigns/{cid}/duplicate", None),
        ("DELETE", f"/email/campaigns/{cid}", None),
        ("POST", f"/email/campaigns/{cid}/steps", {"name": "x", "subject_template": "s"}),
        ("POST", f"/email/campaigns/{cid}/steps/reorder", {"step_ids": step_ids}),
        ("PATCH", f"/email/steps/{step}", {"body_template": "x"}),
        ("DELETE", f"/email/steps/{step}", None),
        ("POST", f"/email/steps/{step}/duplicate", None),
        ("POST", f"/email/steps/{step}/ai-variables", {"key": "x", "prompt_template": "y"}),
        ("PATCH", f"/email/ai-variables/{variable}", {"prompt_template": "y"}),
        ("DELETE", f"/email/ai-variables/{variable}", None),
        (
            "POST",
            "/email/templates/preview",
            {
                "client_profile_id": str(team.lea_profile),
                "step_id": step,
                "subject_template": "s",
                "body_template": "b",
            },
        ),
        (
            "POST",
            f"/email/campaigns/{cid}/members/preview-add",
            {"client_profile_ids": [str(team.max_profile)]},
        ),
        (
            "POST",
            f"/email/campaigns/{cid}/members/add",
            {"client_profile_ids": [str(team.max_profile)]},
        ),
        (
            "POST",
            "/email/members/move",
            {"ids": [str(team.nidria_member)], "target_campaign_id": cid},
        ),
        ("POST", f"/email/campaigns/{cid}/launch", {}),
        ("GET", f"/email/campaigns/{cid}/launch", None),
    ]
    for method, path, body in refused:
        response = await client.request(method, path, json=body, headers=headers)
        assert response.status_code == 409, (method, path, response.text)
        assert response.json()["code"] == "email.campaign_imported", (method, path)
    # Nothing moved, here or at Lemlist.
    assert team.provider.ops == []
    fresh = await campaign_of(db_session, LEMLIST_A)
    assert (fresh.name, len(fresh.steps), fresh.status) == ("2026-08 · Avocats · s2", 2, "PAUSED")
    assert fresh.settings["daily_limit"] == 50 and fresh.settings["sender"] is None
    # A preview of no saved step concerns no campaign: still served.
    plain = await client.post(
        "/email/templates/preview",
        json={
            "client_profile_id": str(team.lea_profile),
            "subject_template": "s",
            "body_template": "b",
        },
        headers=headers,
    )
    assert plain.status_code == 200


async def test_a_pause_a_resume_and_an_archive_are_its_only_gestures(
    client: AsyncClient, db_session: AsyncSession, team: Team, imported: Imported
) -> None:
    headers, provider, cid = team.world.headers, team.provider, imported.campaign_id
    provider.ops.clear()
    resumed = await set_status(client, headers, cid, "ACTIVE")
    assert resumed["status"] == "ACTIVE"
    # Started as it stands at Lemlist: never `start_campaign` and its sender.
    assert provider.names() == ["resume_campaign"]
    await set_status(client, headers, cid, "PAUSED")
    await set_status(client, headers, cid, "ARCHIVED")
    assert provider.names() == ["resume_campaign", "pause_campaign", "archive_campaign"]
    # Archived, it stays archived (no way back for a mirror).
    back = await client.patch(f"/email/campaigns/{cid}", json={"status": "PAUSED"}, headers=headers)
    assert back.status_code == 409 and back.json()["code"] == "email.campaign_imported"


async def test_its_leads_are_paused_resumed_removed_or_moved_to_a_nidria_campaign(
    client: AsyncClient, db_session: AsyncSession, team: Team, imported: Imported
) -> None:
    headers, provider = team.world.headers, team.provider
    ana, ben, gil = (imported.members[k] for k in ("lea_a01", "lea_a02", "lea_a08"))
    provider.ops.clear()
    paused = await act(client, headers, "pause", [ana])
    assert paused["done"] == 1
    # No render is asked: the content is Lemlist's.
    resumed = await act(client, headers, "resume", [ana])
    assert resumed["done"] == 1 and resumed["rows"][0]["status"] == "EMAIL_ACTIVE"
    removed = await act(client, headers, "remove", [gil])
    assert removed["done"] == 1
    moved = await act(
        client, headers, "move", [ben], target_campaign_id=imported.nidria_campaign_id
    )
    assert moved["done"] == 1 and moved["rows"][0]["new_id"]
    assert provider.names() == [
        "pause_contact",
        "resume_contact",
        "remove_contact",
        "remove_contact",
    ]
    target = await db_session.get(EmailCampaignMember, uuid.UUID(moved["rows"][0]["new_id"]))
    assert target is not None and (str(target.campaign_id), target.status) == (
        imported.nidria_campaign_id,
        "EMAIL_READY",
    )


# ── rights and prerequisites ─────────────────────────────────────────────────


def test_creating_client_records_takes_the_permission_of_post_client_profiles() -> None:
    from src.client_profiles.client_profiles_router import BINDINGS

    [binding] = [b for b in BINDINGS if (b.method, b.path) == ("POST", "/client-profiles")]
    assert binding.permission == CLIENT_PROFILE_CREATE == Permission.CASE_EDIT


async def test_an_import_takes_sync_manage_and_creating_client_records(
    client: AsyncClient,
    team: Team,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    agency = team.world.agency_id
    sync = Permission.EMAIL_SYNC
    for held, missing in (
        ((*READ, sync), "email.manage"),
        ((*READ, sync, Permission.EMAIL_MANAGE), "case.edit"),
    ):
        headers = await role_headers(make_role, make_agent, agent_headers, agency, *held)
        for method, body in (("GET", None), ("POST", {"campaign_ids": [LEMLIST_A]})):
            response = await client.request(method, URL, json=body, headers=headers)
            assert response.status_code == 403, (held, method)
            assert response.json()["params"] == {"permission": missing}
    allowed = await role_headers(
        make_role,
        make_agent,
        agent_headers,
        agency,
        *READ,
        sync,
        Permission.EMAIL_MANAGE,
        Permission.CASE_EDIT,
    )
    assert (await client.get(URL, headers=allowed)).status_code == 200
    assert team.provider.names().count("list_campaigns") == 1


async def test_an_import_is_refused_under_impersonation(
    client: AsyncClient,
    team: Team,
    make_agent: MakeAgent,
    system_roles: dict[str, Any],
) -> None:
    target = await make_agent(agency_id=team.world.agency_id, role=system_roles["admin"])
    issued = await client.post(
        f"/agencies/me/members/{target.id}/impersonate", headers=team.world.headers
    )
    assert issued.status_code == 200, issued.text
    mask = {"Authorization": f"Bearer {issued.json()['access_token']}"}
    for method, body in (("GET", None), ("POST", {"campaign_ids": [LEMLIST_A]})):
        refused = await client.request(method, URL, json=body, headers=mask)
        assert refused.status_code == 403 and refused.json()["code"] == "impersonation.denied"
    assert team.provider.ops == []


async def _bound(db: AsyncSession, agency_id: uuid.UUID, team_id: str | None) -> None:
    await _bind(db, agency_id, team_id)


async def test_an_import_needs_the_agency_live_at_lemlist(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    """The dry run cannot import: without real effects, unbound, bound to a
    team the key does not open, or Lemlist not saying which team: the
    module's 409s, before anything is read."""
    headers = email_world.headers

    async def refusal() -> tuple[str, dict[str, Any]]:
        response = await client.get(URL, headers=headers)
        assert response.status_code == 409, response.text
        return response.json()["code"], response.json()["params"]

    assert await refusal() == ("email.provider_unavailable", {"reason": "external_effects_off"})
    with email_env(
        EMAIL_PROVIDER_EXTERNAL_EFFECTS="true", MOCK_LEMLIST="false", LEMLIST_API_KEY="k-test"
    ):
        assert await refusal() == ("email.provider_unavailable", {"reason": "agency_unbound"})
        await _bound(db_session, email_world.agency_id, TEAM_B)
        assert await refusal() == ("email.team_mismatch", {})
        await _bound(db_session, email_world.agency_id, TEAM_A)
        # Lemlist no longer answers which team the key opens (the cached
        # answer forgotten): never a real adapter, a refusal.
        lemlist_team.clear_cache()
        lemlist_team_double.status_code = 503
        assert await refusal() == ("email.provider_unavailable", {"reason": "team_unknown"})
    # Only the team was asked.
    assert {r.url.path for r in lemlist_team_double.requests} == {"/api/team"}


# ── the job, unknown leads ───────────────────────────────────────────────────


async def test_the_job_counts_the_leads_nidria_does_not_know_and_creates_none(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    provider, world = live.provider, live.world
    lemlist_campaign(provider, LEMLIST_A, "2026-08 · Avocats · s2", a_leads())
    provider.events = a_history()
    await import_(client, world.headers, LEMLIST_A)
    campaign = await campaign_of(db_session, LEMLIST_A)
    # Since the import: a lead added at Lemlist, Ana paused there, Fred
    # (paused at Lemlist, listed `emailsSent`) never resumed by the job.
    provider.listed[LEMLIST_A].append(
        ProviderLead("lea_new", None, False, state="emailsSent", raw={"state": "emailsSent"})
    )
    provider.paused.add("lea_a01")
    profiles = await count(db_session, ClientProfile)
    run = await _run(live.sessions)
    assert run.status == "success", run.log_output
    assert run.stats["unknown_leads"] == 1
    assert await count(db_session, ClientProfile) == profiles
    members = await members_by_lead(db_session, campaign.id)
    assert (members["lea_a01"].status, members["lea_a01"].status_reason) == (
        "EMAIL_PAUSED",
        "paused_at_provider",
    )
    assert members["lea_a06"].status == "EMAIL_PAUSED"
    # What the import noted is Nidria's own: the job's reading keeps it.
    assert members["lea_a01"].provider_metadata["import"]["company_name"] == "Acme Avocats"
    detail = (await client.get(f"/email/campaigns/{campaign.id}", headers=world.headers)).json()
    assert detail["unknown_leads"] == 1 and detail["last_imported_at"] is not None
    # Imported again: the new lead is known, nothing unknown remains.
    provider.exports[LEMLIST_A].append(
        ProviderLeadIdentity("lea_new", "new@acme.io", "New", "Lead", None, "running", "emailsSent")
    )
    [row] = (await import_(client, world.headers, LEMLIST_A))["campaigns"]
    assert (row["members_created"], row["unknown_leads"]) == (1, 0)


# ── read only at Lemlist: the Sync, the job, the webhook, the archive ───────


@contextmanager
def _webhook_secret() -> Iterator[None]:
    with email_env(EMAIL_WEBHOOK_SECRET=SECRET):
        yield


def _webhook(
    campaign: str, lead: str, event_id: str, kind: str, step: int | None = 0, **values: Any
) -> dict[str, Any]:
    return {
        "_id": event_id,
        "type": kind,
        "createdAt": "2026-10-08T09:00:00.000Z",
        "campaignId": campaign,
        "leadId": lead,
        "sequenceStep": step,
        "secret": SECRET,
        **values,
    }


#: What a write to an imported campaign would be: never called by these.
WRITES = {
    "create_campaign",
    "update_campaign",
    "start_campaign",
    "resume_campaign",
    "pause_campaign",
    "create_step",
    "update_step",
    "delete_step",
    "park_step",
    "reorder_steps",
    "add_contact",
    "set_step_content",
    "launch_contact",
    "pause_contact",
    "resume_contact",
    "remove_contact",
}


async def test_the_sync_the_job_the_webhook_and_the_archive_only_read_an_imported_campaign(
    client: AsyncClient,
    db_session: AsyncSession,
    live: Live,  # noqa: F811
) -> None:
    """The decision's own test: on an imported campaign, the Sync, the job,
    the webhook and the archive call no provider write but the pause the
    archive asks for (FakeProvider's operations)."""
    provider, world = live.provider, live.world
    lemlist_campaign(provider, LEMLIST_A, "2026-08 · Avocats · s2", a_leads())
    provider.events = a_history()
    await import_(client, world.headers, LEMLIST_A)
    campaign = await campaign_of(db_session, LEMLIST_A)
    cid = str(campaign.id)
    provider.ops.clear()

    synced = await client.post(f"/email/campaigns/{cid}/sync", json={}, headers=world.headers)
    assert synced.status_code == 200, synced.text
    assert synced.json()["unknown_leads"] == 0
    assert synced.json()["members_pushed"] == 0 and synced.json()["steps_created"] == 0
    after_sync = provider.names()
    assert after_sync and set(after_sync) <= READS, after_sync

    run = await _run(live.sessions)
    assert run.status == "success", run.log_output
    assert run.stats["campaigns"] == 1 and run.stats["errors"] == 0
    with _webhook_secret():
        for payload in (
            _webhook(LEMLIST_A, "lea_a01", "act_hook_open", "emailsOpened"),
            _webhook(LEMLIST_A, "lea_a01", "act_hook_pause", "paused", None),
        ):
            ack = await client.post("/webhooks/lemlist", json=payload)
            assert ack.status_code == 200 and ack.json()["stored"] is True
    assert set(provider.names()) <= READS
    await set_status(client, world.headers, cid, "ARCHIVED")

    names = provider.names()
    assert set(names) <= READS | {"archive_campaign"}, names
    assert names.count("archive_campaign") == 1
    assert not set(names) & WRITES
    # The webhook's open went to the imported step's message, Lemlist's.
    opened = await db_session.scalar(
        select(EmailMessage.open_count).where(
            EmailMessage.campaign_id == campaign.id,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.step_position == 1,
            EmailMessage.content_state == "PROVIDER_ONLY",
            EmailMessage.provider_message_id == "act_a01_s",
        )
    )
    assert opened == 2


@pytest_asyncio.fixture
async def adapter_live(
    db_session: AsyncSession,
    email_world: EmailWorld,
    lemlist_live: LemlistTeamDouble,
    job_db: sessionmaker[Session],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[EmailWorld, FakeLemlist, sessionmaker[Session]]:
    """The agency live at Lemlist through the REAL adapter, on `FakeLemlist`:
    every HTTP call is recorded with its method."""
    fake, adapter = lemlist_double(monkeypatch, team_lookup=lambda: TEAM_A)

    def _provider_for(_settings: Any, _row: Any) -> EmailProvider:
        return adapter

    monkeypatch.setattr(email_manager, "provider_for", _provider_for)
    email_manager.clear_caches()
    await _bind(db_session, email_world.agency_id)
    await _enable_job(db_session)
    lead_rows = [
        {"_id": "lea_1", "contactId": "ctc_1", "state": "emailsSent"},
        {"_id": "lea_2", "contactId": "ctc_2", "state": "reviewed"},
    ]
    reply = {
        "_id": "act_2",
        "type": "emailsReplied",
        "campaignId": LEMLIST_A,
        "leadId": "lea_1",
        "contactId": "ctc_1",
        "sequenceStep": 0,
        "createdAt": "2026-10-07T10:00:00.000Z",
    }
    for method, path, body in (
        (
            "GET",
            "/campaigns",
            [
                {
                    "_id": LEMLIST_A,
                    "name": "2026-08 · Avocats · s2",
                    "status": "paused",
                    "createdAt": "2026-08-01T09:00:00.000Z",
                }
            ],
        ),
        (
            "GET",
            f"/campaigns/{LEMLIST_A}",
            {
                "_id": LEMLIST_A,
                "name": "2026-08 · Avocats · s2",
                "status": "paused",
                "sequenceId": "seq_a",
                "timezone": "Europe/Paris",
            },
        ),
        (
            "GET",
            f"/campaigns/{LEMLIST_A}/sequences",
            {
                "seq_a": {
                    "_id": "seq_a",
                    "level": 0,
                    "steps": [
                        {
                            "_id": "stp_1",
                            "type": "email",
                            "delay": 0,
                            "index": 1,
                            "sequenceStep": 0,
                            "subject": "{{firstName}}",
                            "message": "<p>Bonjour</p>",
                        }
                    ],
                }
            },
        ),
        ("GET", f"/campaigns/{LEMLIST_A}/leads", lead_rows),
        (
            "GET",
            f"/v2/campaigns/{LEMLIST_A}/export/leads",
            [
                {
                    "_id": "lea_1",
                    "email": "ana@acme.io",
                    "firstName": "Ana",
                    "lastName": "Lopez",
                    "status": "running",
                    "lastState": "emailsSent",
                },
                {
                    "_id": "lea_2",
                    "email": "ben@acme.io",
                    "firstName": "Ben",
                    "lastName": "Ready",
                    "status": "running",
                    "lastState": "reviewed",
                },
            ],
        ),
        (
            "GET",
            "/activities",
            [
                {
                    "_id": "act_1",
                    "type": "emailsSent",
                    "campaignId": LEMLIST_A,
                    "leadId": "lea_1",
                    "contactId": "ctc_1",
                    "sequenceStep": 0,
                    "createdAt": "2026-10-07T09:00:00.000Z",
                },
                reply,
            ],
        ),
        (
            "GET",
            "/inbox/ctc_1",
            {"data": [{**reply, "message": "<p>Merci, appelons-nous.</p>"}], "pagination": {}},
        ),
        (
            "GET",
            "/leads",
            {"_id": "lea_1", "isPaused": False, "campaignId": LEMLIST_A, "contactId": "ctc_1"},
        ),
        ("POST", f"/campaigns/{LEMLIST_A}/pause", {"_id": LEMLIST_A, "state": "paused"}),
    ):
        fake.answer(method, path, 200, body)
    return email_world, fake, job_db


async def test_at_lemlist_an_imported_campaign_is_only_ever_read_but_for_the_pause_asked(
    client: AsyncClient,
    db_session: AsyncSession,
    adapter_live: tuple[EmailWorld, FakeLemlist, sessionmaker[Session]],
) -> None:
    """The same, at the HTTP level of the real adapter: the import reads
    with GET only; then the Sync, the job, the webhook and the archive send
    one write, the archive's `POST /campaigns/{id}/pause`."""
    world, fake, sessions = adapter_live
    [row] = (await import_(client, world.headers, LEMLIST_A))["campaigns"]
    assert row["ok"] is True and row["members_created"] == 2, row
    assert {method for method, _p, _q, _b in fake.calls} == {"GET"}
    assert (
        "GET",
        f"/v2/campaigns/{LEMLIST_A}/export/leads",
    ) in fake.routes()
    campaign = await campaign_of(db_session, LEMLIST_A)
    cid = str(campaign.id)
    fake.calls.clear()

    synced = await client.post(f"/email/campaigns/{cid}/sync", json={}, headers=world.headers)
    assert synced.status_code == 200, synced.text
    run = await _run(sessions)
    assert run.status == "success", run.log_output
    assert run.stats["campaigns"] == 1 and run.stats["errors"] == 0, run.stats
    with _webhook_secret():
        for payload in (
            _webhook(LEMLIST_A, "lea_1", "act_hook_open", "emailsOpened"),
            _webhook(LEMLIST_A, "lea_1", "act_hook_pause", "paused", None),
        ):
            ack = await client.post("/webhooks/lemlist", json=payload)
            assert ack.status_code == 200 and ack.json()["stored"] is True
    await set_status(client, world.headers, cid, "ARCHIVED")

    writes = [(method, path) for method, path, _q, _b in fake.calls if method != "GET"]
    assert writes == [("POST", f"/campaigns/{LEMLIST_A}/pause")]
    read_routes = {path for method, path, _q, _b in fake.calls if method == "GET"}
    assert {
        f"/campaigns/{LEMLIST_A}",
        f"/campaigns/{LEMLIST_A}/leads",
        "/activities",
    } <= read_routes
    assert "/leads" in read_routes and "/inbox/ctc_1" in read_routes
