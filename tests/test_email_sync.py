"""The manual Sync and the Launch's push (spec 10.1, 10.2, 8.5): the campaign
and its steps at Lemlist, each lead's exact emails pushed before it is
launched, a template edit reaching only the emails not sent yet, the dry
run dropped when real effects come, the Lemlist team read before a real
campaign exists, and, above all, NO NEW LEAD EVER HANDED OVER BY A SYNC.

Ported from Prism's `tests/test_email_campaigns.py` (reference 629a2b0), its
sync part; the events a real sync reads are phase 4's ingestion, stood in
for here by the email plugin's factories where a test needs them.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import (
    EmailCampaign,
    EmailCampaignMember,
    EmailCampaignStep,
    EmailMemberRender,
)
from src.email_campaigns import email_manager
from src.email_campaigns.email_manager import prompt_hash
from src.email_campaigns.providers.base import EmailProvider, LeadRef, ProviderError
from src.email_campaigns.providers.dry_run import DryRunEmailProvider
from src.email_campaigns.renderer import body_to_html
from tests.email_fakes import FakeProvider
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import EmailWorld, MakeCompanyLink
from tests.test_email_campaigns import act, campaign_via_api, members_of, set_status, step_via_api
from tests.test_email_launch import (
    add_via_api,
    ai_calls,  # noqa: F401 (a fixture)
    launch_via_api,
    ready_campaign,
    sync_via_api,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

#: What a sync may never call: only a Launch hands a lead over (spec 8.5).
HAND_OVER = {"add_contact", "launch_contact"}


async def _synced(client: AsyncClient, world: EmailWorld, cid: str, **body: Any) -> dict[str, Any]:
    response = await sync_via_api(client, world.headers, cid, **body)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _launched(
    client: AsyncClient, world: EmailWorld, cid: str, **body: Any
) -> dict[str, Any]:
    response = await launch_via_api(client, world.headers, cid, **body)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _campaign(db: AsyncSession, cid: str) -> EmailCampaign:
    campaign = await db.scalar(
        select(EmailCampaign)
        .where(EmailCampaign.id == uuid.UUID(cid))
        .execution_options(populate_existing=True)
    )
    assert campaign is not None
    return campaign


# ── the dry run ──────────────────────────────────────────────────────────────


async def test_a_dry_run_sync_creates_the_campaign_never_a_lead_and_locks_the_sequence(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
) -> None:
    meta = (await client.get("/email/meta", headers=email_world.headers)).json()
    assert meta["provider_mode"] == "DRY_RUN"
    cid, _, _ = await ready_campaign(
        client,
        db_session,
        email_world,
        make_client_profile,
        ("Ana Ready", "Ben Ready"),
        activate=False,
    )

    # A draft is synced (refreshed), never launched: its leads wait.
    draft = await _synced(client, email_world, cid)
    assert draft["external"] is False and draft["campaign_created"] is True
    assert (draft["steps_created"], draft["members_pushed"], draft["remaining"]) == (2, 0, 2)
    assert draft["notes"] == [{"code": "ready_to_launch", "params": {"count": 2}}]
    assert draft["last_synced_at"] is not None
    # A dry-run campaign belongs to no Lemlist team.
    assert (await _campaign(db_session, cid)).provider_metadata == {}

    await client.patch(
        f"/email/campaigns/{cid}",
        json={"settings": {"daily_limit": 1}},
        headers=email_world.headers,
    )
    await set_status(client, email_world.headers, cid, "ACTIVE")
    first = await _launched(client, email_world, cid)
    assert (first["members_pushed"], first["remaining"], first["pushed_today"]) == (1, 1, 1)
    assert first["notes"][0]["code"] == "daily_limit_reached"
    await client.patch(
        f"/email/campaigns/{cid}",
        json={"settings": {"daily_limit": 50}},
        headers=email_world.headers,
    )
    second = await _launched(client, email_world, cid)
    assert second["members_pushed"] == 1 and second["remaining"] == 0

    members = await members_of(db_session, cid)
    assert {m["status"] for m in members} == {"EMAIL_QUEUED"}
    assert all(m["provider_lead_id"].startswith("dry_") for m in members)
    renders = await db_session.scalar(
        select(func.count()).where(
            EmailMemberRender.campaign_member_id.in_([m["id"] for m in members]),
            EmailMemberRender.provider_synced_at.is_not(None),
        )
    )
    assert renders == 4  # two leads by two steps, each frozen before the hand-over

    detail = (await client.get(f"/email/campaigns/{cid}", headers=email_world.headers)).json()
    assert detail["provider_mode"] == "DRY_RUN"
    # The provider holds the leads: its sequence only grows at the end.
    sequence = detail["sequence"]
    assert sequence["active_sequence"] is True
    assert (sequence["can_add_step"], sequence["can_edit_step_content"]) == (True, True)
    assert (sequence["can_insert_step"], sequence["can_reorder_steps"]) == (False, False)
    step_ids = [s["id"] for s in detail["steps"]]
    for response, code in (
        (
            await client.post(
                f"/email/campaigns/{cid}/steps",
                json={"name": "x", "position": 1, "subject_template": "s"},
                headers=email_world.headers,
            ),
            "email.step_insert_in_use",
        ),
        (
            await client.delete(f"/email/steps/{step_ids[0]}", headers=email_world.headers),
            "email.step_delete_in_use",
        ),
        (
            await client.post(
                f"/email/campaigns/{cid}/steps/reorder",
                json={"step_ids": list(reversed(step_ids))},
                headers=email_world.headers,
            ),
            "email.sequence_in_use",
        ),
    ):
        assert response.status_code == 409 and response.json()["code"] == code, response.text
    edited = await client.patch(
        f"/email/steps/{step_ids[1]}",
        json={"body_template": "Une relance."},
        headers=email_world.headers,
    )
    assert edited.status_code == 200 and edited.json()["unsynced"] is True
    leads = (
        await client.get("/email/members", params={"campaign_id": cid}, headers=email_world.headers)
    ).json()
    assert leads["total"] == 2 and all(lead["dry_run"] for lead in leads["items"])


# ── the real path ────────────────────────────────────────────────────────────


async def test_a_launch_pushes_each_leads_exact_emails_before_launching_it(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    ai_calls: list[str],  # noqa: F811
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
) -> None:
    agency = email_world.agency_id
    ana = await make_client_profile(
        agency_id=agency, first_name="Ana", last_name="Lopez", email="ana@acme.io"
    )
    ben = await make_client_profile(
        agency_id=agency, first_name="Ben", last_name="Ready", email="ben@acme.io"
    )
    ana_id, ben_id = ana.id, ben.id
    await make_company_link(client_profile_id=ana_id, agency_id=agency, name="Ana Corp")
    cid = (await campaign_via_api(client, email_world.headers))["id"]
    step1 = (
        await step_via_api(
            client,
            email_world.headers,
            cid,
            name="Email initial",
            subject_template='{{client.first_name}}, une question sur {{company.name | "vous"}}',
            body_template="Bonjour {{client.first_name}},\n\n{{ai.context}}\nwww.nidria.app",
        )
    )["steps"][0]
    prompt = "Une phrase sur {{company.name}}."
    created = await client.post(
        f"/email/steps/{step1['id']}/ai-variables",
        json={"key": "context", "prompt_template": prompt},
        headers=email_world.headers,
    )
    assert created.status_code == 201, created.text
    await step_via_api(
        client,
        email_world.headers,
        cid,
        name="Relance",
        subject_template="",
        body_template="Un rappel.",
    )
    await add_via_api(client, email_world.headers, cid, client_profile_ids=[ana_id, ben_id])
    await set_status(client, email_world.headers, cid, "ACTIVE")

    result = await _launched(client, email_world, cid)
    assert result["external"] is True and result["members_pushed"] == 2, result
    names = fake_email_provider.names()
    # Lemlist starts the campaign once its steps exist (and gives it a sender).
    assert names[:4] == ["create_campaign", "create_step", "create_step", "start_campaign"]
    assert names.count("add_contact") == 2 and names.count("launch_contact") == 2
    assert names.count("set_step_content") == 4
    # The team the key opens was read before the campaign existed, and noted.
    campaign = await _campaign(db_session, cid)
    assert campaign.provider_metadata == {"team_id": "tea_current"}
    assert campaign.provider_campaign_id == "cam_1" and campaign.sync_error is None
    # Every content is pushed before its lead is launched.
    for index, (name, kw) in enumerate(fake_email_provider.ops):
        if name != "launch_contact":
            continue
        pushes = [
            i
            for i, (n, k) in enumerate(fake_email_provider.ops)
            if n == "set_step_content" and k["lead"] == kw["lead"]
        ]
        assert len(pushes) == 2 and max(pushes) < index
    # The follow-up keeps its empty subject: it stays in the thread.
    subjects = sorted(kw["subject"] for n, kw in fake_email_provider.ops if n == "set_step_content")
    assert subjects[:2] == ["", ""]

    members = {m["client_profile_id"]: m for m in await members_of(db_session, cid)}
    ana_lead = members[ana_id]["provider_lead_id"]
    render = await db_session.scalar(
        select(EmailMemberRender).where(
            EmailMemberRender.campaign_member_id == members[ana_id]["id"],
            EmailMemberRender.step_position == 1,
        )
    )
    assert render is not None
    step = await db_session.get(EmailCampaignStep, uuid.UUID(step1["id"]))
    assert step is not None and step.provider_step_id
    pushed = fake_email_provider.contents[(ana_lead, step.provider_step_id)]
    # Exactly the render Nidria keeps: its subject, its body as HTML.
    assert pushed.subject == render.subject == "Ana, une question sur Ana Corp"
    assert pushed.html == body_to_html(render.body)
    assert '<a href="https://www.nidria.app">' in pushed.html
    assert render.ai_values == {
        "context": {"value": "Une phrase.", "prompt_hash": prompt_hash(prompt, None)}
    }
    assert ai_calls == ["context", "context"]
    ben_render = await db_session.scalar(
        select(EmailMemberRender.subject).where(
            EmailMemberRender.campaign_member_id == members[ben_id]["id"],
            EmailMemberRender.step_position == 1,
        )
    )
    assert ben_render == "Ben, une question sur vous"  # the fallback, recorded


async def test_a_template_edit_reaches_only_the_emails_not_sent_yet(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, profiles, members = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez", "Ben Ready")
    )
    await _launched(client, email_world, cid)
    # What phase 4's ingestion would have written: Ana was sent step 1 and
    # replied; Ben is queued.
    ana = members["Ana Lopez"]
    await db_session.execute(
        update(EmailCampaignMember)
        .where(EmailCampaignMember.id == ana)
        .values(status="EMAIL_REPLIED", current_step=1, started_at=datetime.now(UTC))
    )
    await db_session.execute(
        update(EmailMemberRender)
        .where(EmailMemberRender.campaign_member_id == ana, EmailMemberRender.step_position == 1)
        .values(sent_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await db_session.commit()
    steps = (await client.get(f"/email/campaigns/{cid}/steps", headers=email_world.headers)).json()
    ben = members["Ben Ready"]
    ben_lead = next(m for m in await members_of(db_session, cid) if m["id"] == ben)[
        "provider_lead_id"
    ]
    # Lemlist already sent Ben his first step: that email stays as it was.
    fake_email_provider.reached.add((ben_lead, steps[0]["provider_step_id"]))
    for step in steps:
        await client.patch(
            f"/email/steps/{step['id']}",
            json={"body_template": step["body_template"] + "\n\nP.S."},
            headers=email_world.headers,
        )
    fake_email_provider.ops.clear()
    refreshed = await _synced(client, email_world, cid)
    assert refreshed["members_updated"] == 1 and refreshed["errors"] == []
    assert HAND_OVER.isdisjoint(fake_email_provider.names())
    renders = {
        r.step_position: r
        for r in (
            await db_session.scalars(
                select(EmailMemberRender)
                .where(EmailMemberRender.campaign_member_id == ben)
                .execution_options(populate_existing=True)
            )
        ).all()
    }
    assert not renders[1].body.endswith("P.S.") and renders[2].body.endswith("P.S.")
    rows = {m["id"]: m for m in await members_of(db_session, cid)}
    assert rows[ben]["current_step"] == 1  # Lemlist said it reached step 1
    # Ana's sent email never moved, and she was never asked of Lemlist again.
    pushed_leads = {kw["lead"] for n, kw in fake_email_provider.ops if n == "set_step_content"}
    assert pushed_leads == {ben_lead}
    detail = (await client.get(f"/email/campaigns/{cid}", headers=email_world.headers)).json()
    assert detail["unsynced_changes"] is False
    assert profiles


async def test_the_team_is_read_before_a_real_campaign_is_created(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """Lemlist does not say which team the key opens: no campaign is
    created there (it would be read only for good, decision A4)."""
    cid, _, _ = await ready_campaign(client, db_session, email_world, make_client_profile)
    fake_email_provider.team = None
    for response in (
        await sync_via_api(client, email_world.headers, cid),
        await launch_via_api(client, email_world.headers, cid),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "email.provider_unavailable"
        assert response.json()["params"] == {"reason": "team_unknown"}
    assert fake_email_provider.ops == []
    campaign = await _campaign(db_session, cid)
    assert campaign.provider_campaign_id is None
    assert campaign.sync_error == "email.provider_unavailable"


async def test_a_campaign_not_active_is_created_paused_and_synced_in_batches(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, members = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez", "Ben Ready", "Cleo Doe")
    )
    await set_status(client, email_world.headers, cid, "PAUSED")
    synced = await _synced(client, email_world, cid)
    # However Lemlist creates it, it mirrors Nidria from the start: paused.
    assert fake_email_provider.names() == [
        "create_campaign",
        "pause_campaign",
        "create_step",
        "create_step",
    ]
    assert synced["campaign_created"] is True
    await set_status(client, email_world.headers, cid, "ACTIVE")
    await _launched(client, email_world, cid)
    # Every step edited: three running leads to refresh, one per call.
    steps = (await client.get(f"/email/campaigns/{cid}/steps", headers=email_world.headers)).json()
    for step in steps:
        await client.patch(
            f"/email/steps/{step['id']}",
            json={"body_template": "Neuf."},
            headers=email_world.headers,
        )
    remaining = []
    for _ in range(4):
        result = await _synced(client, email_world, cid, limit=1)
        remaining.append((result["members_updated"], result["updates_remaining"]))
    assert remaining == [(1, 2), (1, 1), (1, 0), (0, 0)]
    assert members


async def test_turning_real_effects_on_replaces_what_the_dry_run_simulated(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid, _, members = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez", "Ben Ready")
    )
    dry = await _launched(client, email_world, cid)
    assert dry["external"] is False and dry["members_pushed"] == 2
    # An agent pauses Ben in the dry run: that pause is the agent's, it stays.
    await act(client, email_world.headers, "pause", [members["Ben Ready"]])

    fake = FakeProvider()

    def real(_settings: Any, _row: Any) -> EmailProvider:
        return fake

    monkeypatch.setattr(email_manager, "provider_for", real)
    live = await _launched(client, email_world, cid)
    assert live["campaign_created"] is True and live["members_pushed"] == 1
    rows = {m["id"]: m for m in await members_of(db_session, cid)}
    ana, ben = rows[members["Ana Lopez"]], rows[members["Ben Ready"]]
    assert ana["status"] == "EMAIL_QUEUED" and ana["provider_lead_id"].startswith("lea_")
    assert (ben["status"], ben["provider_lead_id"]) == ("EMAIL_PAUSED", None)
    campaign = await _campaign(db_session, cid)
    assert campaign.provider_campaign_id == "cam_1"
    assert not any(
        s.provider_step_id is None or s.provider_step_id.startswith("dry_")
        for s in (
            await db_session.scalars(
                select(EmailCampaignStep)
                .where(EmailCampaignStep.campaign_id == uuid.UUID(cid))
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    assert fake.names().count("add_contact") == 1

    # Live at Lemlist, then the agency back in dry run: Nidria refuses to diverge.
    def dry_again(_settings: Any, _row: Any) -> EmailProvider:
        return DryRunEmailProvider()

    monkeypatch.setattr(email_manager, "provider_for", dry_again)
    email_manager.clear_caches()
    refused = await launch_via_api(client, email_world.headers, cid)
    assert refused.status_code == 409
    assert (refused.json()["code"], refused.json()["params"]) == (
        "email.provider_unavailable",
        {"reason": "external_effects_off"},
    )
    paused = await act(client, email_world.headers, "pause", [members["Ana Lopez"]])
    assert paused["failed"] == 1
    assert paused["rows"][0]["code"] == "email.provider_unavailable"
    rows = {m["id"]: m for m in await members_of(db_session, cid)}
    assert rows[members["Ana Lopez"]]["status"] == "EMAIL_QUEUED"


async def test_the_campaign_gates_refuse_a_sync_with_their_code(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
) -> None:
    empty = (await campaign_via_api(client, email_world.headers, "Empty"))["id"]
    response = await sync_via_api(client, email_world.headers, empty)
    assert (response.status_code, response.json()["code"]) == (400, "email.no_steps")
    archived = (await campaign_via_api(client, email_world.headers, "Archived"))["id"]
    await step_via_api(client, email_world.headers, archived)
    await set_status(client, email_world.headers, archived, "ARCHIVED")
    for response in (
        await sync_via_api(client, email_world.headers, archived),
        await launch_via_api(client, email_world.headers, archived),
    ):
        assert (response.status_code, response.json()["code"]) == (400, "email.campaign_archived")
    assert fake_email_provider.ops == []
    invalid = await sync_via_api(client, email_world.headers, empty, limit=0)
    assert invalid.status_code == 422


# ── the Sync never hands a new lead over (spec 8.5) ──────────────────────────


async def test_a_sync_never_adds_nor_launches_a_lead_whatever_the_campaign_holds(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """Whatever the campaign holds (READY leads never handed over, a lead
    Lemlist added but whose launch its plan refused, running leads, a
    follow-up to add, an edited template) and whatever its state, a Sync
    calls neither the lead creation nor the launch. Only a Launch does."""
    cid, profiles, members = await ready_campaign(
        client,
        db_session,
        email_world,
        make_client_profile,
        ("Ana Lopez", "Ben Ready", "Cleo Doe", "Dan Waiting"),
    )
    synced: list[str] = []

    async def sync_and_check(**body: Any) -> dict[str, Any]:
        fake_email_provider.ops.clear()
        result = await _synced(client, email_world, cid, **body)
        names = fake_email_provider.names()
        assert HAND_OVER.isdisjoint(names), names
        assert result["members_pushed"] == 0 and result["remaining"] >= 0
        synced.extend(names)
        return result

    # A draft, then active: nothing handed over, four leads wait.
    first = await sync_and_check()
    assert first["remaining"] == 4
    # Ana launched; Ben added and personalized but refused by the plan.
    await _launched(client, email_world, cid, member_ids=[members["Ana Lopez"]])

    def refused(lead: LeadRef) -> None:
        raise ProviderError("Upgrade your plan", code="PLAN_REQUIRED", status=402)

    original = fake_email_provider.launch_contact
    fake_email_provider.launch_contact = refused  # type: ignore[method-assign]
    await _launched(client, email_world, cid, member_ids=[members["Ben Ready"]])
    fake_email_provider.launch_contact = original  # type: ignore[method-assign]
    ben = next(m for m in await members_of(db_session, cid) if m["id"] == members["Ben Ready"])
    assert ben["provider_lead_id"] is not None and ben["status"] == "EMAIL_READY"

    # Active, a follow-up added and a template edited: still nothing.
    await step_via_api(client, email_world.headers, cid, name="Encore", subject_template="")
    steps = (await client.get(f"/email/campaigns/{cid}/steps", headers=email_world.headers)).json()
    await client.patch(
        f"/email/steps/{steps[0]['id']}",
        json={"body_template": "Neuf."},
        headers=email_world.headers,
    )
    active = await sync_and_check(limit=1)
    assert active["remaining"] == 3  # Ben (added, never launched), Cleo, Dan
    assert "set_step_content" in synced and "create_step" in synced
    # Paused, then active again, batch after batch: still nothing.
    await set_status(client, email_world.headers, cid, "PAUSED")
    await sync_and_check()
    await set_status(client, email_world.headers, cid, "ACTIVE")
    for _ in range(3):
        await sync_and_check(limit=1)

    rows = {m["id"]: m for m in await members_of(db_session, cid)}
    assert rows[members["Ben Ready"]]["status"] == "EMAIL_READY"
    for person in ("Cleo Doe", "Dan Waiting"):
        assert rows[members[person]]["status"] == "EMAIL_READY", person
        assert rows[members[person]]["provider_lead_id"] is None, person
    queued = await db_session.scalar(
        select(func.count())
        .select_from(EmailCampaignMember)
        .where(
            EmailCampaignMember.campaign_id == uuid.UUID(cid),
            EmailCampaignMember.queued_at.is_not(None),
        )
    )
    assert queued == 1  # Ana, and only through the Launch
    assert profiles
