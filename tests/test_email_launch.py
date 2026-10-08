"""Launch: the only way a new lead reaches Lemlist (spec 8.5), checked by the
shared rules (readiness) at the moment of the hand-over: the same rules the
Launch page reads without asking Lemlist for its leads and without a model.

Ported from Prism's `tests/test_email_launch.py` (reference 629a2b0). The
legacy workflow's cases become Nidria's DEMO_RECIPIENT (rule N11); a
TEAM_MISMATCH case is added (rule N4), so are the plan refusal (402), the
step Lemlist already reached and the data a lead carries (decision A8).

No test reaches Lemlist (`FakeProvider`, or the dry run) and no test calls a
model: AI values come from phase 5's seam (`generate_ai_values`), replaced
and counted here; a read generates none. Helpers shared with the other 3B
test files live here.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.client_profile import ClientProfile
from shared.models.email_campaign import EmailCampaign, EmailCampaignMember, EmailMemberRender
from src.email_campaigns.email_manager import EmailManager
from src.email_campaigns.providers.base import (
    CampaignRef,
    LeadRef,
    LeadSpec,
    ProviderError,
)
from tests.email_fakes import FakeProvider
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import EmailWorld, MakeCompanyLink, MakeEmailMember
from tests.test_email_campaigns import (
    act,
    campaign_via_api,
    make_live,
    members_of,
    set_status,
    step_via_api,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

DEMO = "demo+relocation-lisboa-1@nidria.app"


# ── helpers (shared with the other 3B test files) ────────────────────────────


async def add_via_api(
    client: AsyncClient, headers: dict[str, str], campaign_id: Any, **body: Any
) -> dict[str, Any]:
    """The wizard's add: always the preview's verdicts, row by row."""
    payload = {
        key: [str(i) for i in value] if isinstance(value, list) else value
        for key, value in body.items()
    }
    response = await client.post(
        f"/email/campaigns/{campaign_id}/members/add", json=payload, headers=headers
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def launch_via_api(
    client: AsyncClient, headers: dict[str, str], campaign_id: Any, **body: Any
) -> Response:
    if "member_ids" in body:
        body["member_ids"] = [str(i) for i in body["member_ids"]]
    return await client.post(f"/email/campaigns/{campaign_id}/launch", json=body, headers=headers)


async def sync_via_api(
    client: AsyncClient, headers: dict[str, str], campaign_id: Any, **body: Any
) -> Response:
    return await client.post(f"/email/campaigns/{campaign_id}/sync", json=body, headers=headers)


async def readiness_of(
    client: AsyncClient, headers: dict[str, str], campaign_id: Any, **params: Any
) -> dict[str, Any]:
    response = await client.get(
        f"/email/campaigns/{campaign_id}/launch", params=params, headers=headers
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def by_name(readiness: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["lead"]["full_name"]: row for row in readiness["items"]}


def codes(row: dict[str, Any]) -> set[str]:
    return {issue["code"] for issue in row["issues"]}


async def ready_campaign(
    client: AsyncClient,
    db: AsyncSession,
    world: EmailWorld,
    make_client_profile: MakeClientProfile,
    people: tuple[str, ...] = ("Ana Lopez", "Ben Ready", "Cleo Doe"),
    *,
    activate: bool = True,
    name: str = "Founders",
    **settings: Any,
) -> tuple[str, dict[str, uuid.UUID], dict[str, uuid.UUID]]:
    """A campaign of two steps with its people READY: (campaign id, full
    name -> client record id, full name -> membership id)."""
    profiles: dict[str, uuid.UUID] = {}
    for person in people:
        first, last = person.split()
        profile = await make_client_profile(
            agency_id=world.agency_id,
            first_name=first,
            last_name=last,
            email=f"{first.lower()}@acme.io",
        )
        profiles[person] = profile.id
    cid = (await campaign_via_api(client, world.headers, name))["id"]
    if settings:
        patched = await client.patch(
            f"/email/campaigns/{cid}", json={"settings": settings}, headers=world.headers
        )
        assert patched.status_code == 200, patched.text
    await step_via_api(client, world.headers, cid, name="Intro")
    await step_via_api(client, world.headers, cid, name="Relance", subject_template="")
    added = await add_via_api(
        client, world.headers, cid, client_profile_ids=list(profiles.values())
    )
    assert added["added"] == len(people), added
    if activate:
        await set_status(client, world.headers, cid, "ACTIVE")
    rows = {m["client_profile_id"]: m["id"] for m in await members_of(db, cid)}
    return cid, profiles, {name: rows[pid] for name, pid in profiles.items()}


@pytest.fixture
def ai_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Phase 5's seam stood in for: every AI value generated is recorded
    and is a plain sentence; nothing reaches a model."""
    calls: list[str] = []

    async def generated(
        self: EmailManager, access: Any, profile: Any, people: Any, variables: Any
    ) -> tuple[dict[str, str], dict[str, str]]:
        calls.extend(key for key, _, _ in variables)
        return {key: "Une phrase." for key, _, _ in variables}, {}

    monkeypatch.setattr(EmailManager, "generate_ai_values", generated)
    # AI generation configured: an AI passage is written at launch (a warning).
    monkeypatch.setattr(EmailManager, "ai_configured", lambda self: True)
    return calls


# ── the Launch page: a read, the hand-over's own rules ───────────────────────


async def test_readiness_names_every_blocking_reason_without_provider_or_model(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    ai_calls: list[str],
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
) -> None:
    people = ("Ana Ready", "Ben Blank", "Cleo Odd", "Dan Demo", "Eve Bounced", "Fay Paused")
    cid, profiles, members = await ready_campaign(
        client, db_session, email_world, make_client_profile, people
    )
    earlier = (await campaign_via_api(client, email_world.headers, "Earlier"))["id"]
    # What changed since they were added: an address cleared, another
    # malformed, a demo one (rule N11), a bounce in an earlier campaign, a pause.
    for person, email in (
        ("Ben Blank", None),
        ("Cleo Odd", "cleo at acme"),
        ("Dan Demo", DEMO),
    ):
        await db_session.execute(
            update(ClientProfile).where(ClientProfile.id == profiles[person]).values(email=email)
        )
    await db_session.commit()
    await make_email_member(
        campaign_id=uuid.UUID(earlier),
        client_profile_id=profiles["Eve Bounced"],
        agency_id=email_world.agency_id,
        status="EMAIL_BOUNCED",
    )
    paused = await act(client, email_world.headers, "pause", [members["Fay Paused"]])
    assert paused["done"] == 1
    ops = len(fake_email_provider.ops)

    readiness = await readiness_of(client, email_world.headers, cid)
    rows = by_name(readiness)
    assert rows["Ana Ready"]["ready"] is True
    assert {i["code"] for i in rows["Ana Ready"]["issues"] if i["blocking"]} == set()
    for person, code in (
        ("Ben Blank", "NO_EMAIL"),
        ("Cleo Odd", "INVALID_EMAIL"),
        ("Dan Demo", "DEMO_RECIPIENT"),
        ("Eve Bounced", "BOUNCED"),
        ("Fay Paused", "PAUSED"),
    ):
        assert code in codes(rows[person]) and rows[person]["ready"] is False, person
    assert (readiness["to_launch"], readiness["ready"], readiness["blocked"]) == (6, 1, 5)
    assert readiness["ready_ids"] == [str(members["Ana Ready"])]
    assert readiness["can_launch"] is True
    # A read: Lemlist was not asked anything, no model either.
    assert len(fake_email_provider.ops) == ops and ai_calls == []

    blocked = await readiness_of(client, email_world.headers, cid, readiness="blocked")
    assert blocked["total"] == 5 and all(not row["ready"] for row in blocked["items"])
    ready = await readiness_of(client, email_world.headers, cid, readiness="ready")
    assert [row["lead"]["full_name"] for row in ready["items"]] == ["Ana Ready"]
    searched = await readiness_of(client, email_world.headers, cid, search="cleo")
    assert [row["lead"]["full_name"] for row in searched["items"]] == ["Cleo Odd"]
    paged = await readiness_of(client, email_world.headers, cid, page=2, page_size=4)
    assert (paged["total"], len(paged["items"])) == (6, 2)

    # The Leads list shows the same reasons on the same leads.
    leads = (
        await client.get("/email/members", params={"campaign_id": cid}, headers=email_world.headers)
    ).json()["items"]
    shown = {row["full_name"]: {i["code"] for i in row["issues"]} for row in leads}
    assert "DEMO_RECIPIENT" in shown["Dan Demo"] and "BOUNCED" in shown["Eve Bounced"]


async def test_readiness_reads_the_templates_and_the_campaign_gates(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    ai_calls: list[str],
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
) -> None:
    cid, profiles, members = await ready_campaign(
        client,
        db_session,
        email_world,
        make_client_profile,
        ("Ana Lopez", "Ben Nosite"),
        activate=False,
    )
    # A draft: nobody launches, the campaign says why.
    draft = await readiness_of(client, email_world.headers, cid)
    assert draft["can_launch"] is False
    assert [g["code"] for g in draft["gates"] if g["blocking"]] == ["NOT_ACTIVE"]
    assert any(g["code"] == "NO_SENDER" and not g["blocking"] for g in draft["gates"])

    # A third step needs the company's website (no fallback) and an AI passage.
    await make_company_link(
        client_profile_id=profiles["Ana Lopez"],
        agency_id=email_world.agency_id,
        name="Ana Corp",
        custom_fields={"website": "https://ana.example"},
    )
    await make_company_link(
        client_profile_id=profiles["Ben Nosite"], agency_id=email_world.agency_id, name="Ben Corp"
    )
    third = await step_via_api(
        client,
        email_world.headers,
        cid,
        name="Dernier mot",
        subject_template="",
        body_template="{{client.first_name}}, vu sur {{company.website}}.\n\n{{ai.hook}}",
    )
    step_id = next(s["id"] for s in third["steps"] if s["position"] == 3)
    created = await client.post(
        f"/email/steps/{step_id}/ai-variables",
        json={"key": "hook", "prompt_template": "Une phrase sur {{company.name}}."},
        headers=email_world.headers,
    )
    assert created.status_code == 201, created.text
    await set_status(client, email_world.headers, cid, "ACTIVE")

    readiness = await readiness_of(client, email_world.headers, cid)
    rows = by_name(readiness)
    [template] = [i for i in rows["Ben Nosite"]["issues"] if i["code"] == "TEMPLATE"]
    assert template["blocking"] is True
    assert template["params"] == {
        "position": 3,
        "kind": "missing_variable",
        "token": "company.website",
    }
    # The AI passage is written at launch: a warning, never a block, never a call.
    ana = rows["Ana Lopez"]
    assert ana["ready"] is True
    assert any(i["code"] == "AI_AT_LAUNCH" and not i["blocking"] for i in ana["issues"])
    assert (ana["steps_rendered"], ana["steps_total"]) == (0, 3)
    assert readiness["can_launch"] is True
    assert ai_calls == []

    # The daily limit, reached, closes the day for everyone.
    patched = await client.patch(
        f"/email/campaigns/{cid}",
        json={"settings": {"daily_limit": 1}},
        headers=email_world.headers,
    )
    assert patched.status_code == 200
    launched = (await launch_via_api(client, email_world.headers, cid)).json()
    assert launched["members_pushed"] == 1, launched
    # Only the launch wrote the AI passage, once, for the lead it launched.
    assert ai_calls == ["hook"]
    closed = await readiness_of(client, email_world.headers, cid)
    assert closed["can_launch"] is False
    [gate] = [g for g in closed["gates"] if g["blocking"]]
    assert gate == {
        "code": "DAILY_LIMIT",
        "params": {"launched_today": 1, "daily_limit": 1},
        "blocking": True,
    }
    assert (closed["launched_today"], closed["remaining_today"], closed["launched"]) == (1, 0, 1)
    assert members


async def test_an_ai_passage_blocks_the_launch_page_and_the_launch_alike_without_ai(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """Adjustment b (decision A14): AI generation is not configured, and the
    first email uses `{{ai.hook}}`. The Launch page shows the lead BLOCKED
    by AI_AT_LAUNCH (`reason: ai_not_configured`), and the Launch refuses it
    with that very code, before any call to Lemlist."""
    cid, _, members = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez",)
    )
    steps = (await client.get(f"/email/campaigns/{cid}/steps", headers=email_world.headers)).json()
    first = steps[0]["id"]
    created = await client.post(
        f"/email/steps/{first}/ai-variables",
        json={"key": "hook", "prompt_template": "Une phrase sur {{client.first_name}}."},
        headers=email_world.headers,
    )
    assert created.status_code == 201, created.text
    await client.patch(
        f"/email/steps/{first}",
        json={"body_template": "Bonjour {{client.first_name}},\n\n{{ai.hook}}"},
        headers=email_world.headers,
    )
    readiness = await readiness_of(client, email_world.headers, cid)
    ana = by_name(readiness)["Ana Lopez"]
    assert ana["ready"] is False
    [issue] = [i for i in ana["issues"] if i["code"] == "AI_AT_LAUNCH"]
    assert issue == {
        "code": "AI_AT_LAUNCH",
        "params": {"reason": "ai_not_configured", "position": 1},
        "blocking": True,
    }
    assert (readiness["ready"], readiness["blocked"]) == (0, 1)

    fake_email_provider.ops.clear()
    launched = (await launch_via_api(client, email_world.headers, cid)).json()
    assert (launched["members_pushed"], launched["members_failed"]) == (0, 1)
    [failure] = launched["failures"]
    assert (failure["code"], failure["params"]) == (
        "AI_AT_LAUNCH",
        {"reason": "ai_not_configured", "position": 1},
    )
    assert {"add_contact", "set_step_content", "launch_contact"}.isdisjoint(
        fake_email_provider.names()
    )
    rows = {m["id"]: m for m in await members_of(db_session, cid)}
    assert rows[members["Ana Lopez"]]["status"] == "EMAIL_READY"
    assert rows[members["Ana Lopez"]]["status_reason"] == "AI_AT_LAUNCH"


# ── launching: selected, all eligible, idempotent, partial ───────────────────


async def test_launch_selected_hands_over_only_those_and_a_second_click_does_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, members = await ready_campaign(client, db_session, email_world, make_client_profile)
    chosen = members["Ben Ready"]

    first = await launch_via_api(client, email_world.headers, cid, member_ids=[chosen])
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["launched_ids"] == [str(chosen)] and body["members_pushed"] == 1
    assert body["skipped"] == [] and body["failures"] == []
    assert fake_email_provider.names().count("add_contact") == 1
    rows = {m["id"]: m for m in await members_of(db_session, cid)}
    assert rows[chosen]["status"] == "EMAIL_QUEUED"
    assert rows[members["Ana Lopez"]]["status"] == "EMAIL_READY"

    # A double click: the lead is launched already, nothing reaches Lemlist.
    again = (await launch_via_api(client, email_world.headers, cid, member_ids=[chosen])).json()
    assert again["members_pushed"] == 0 and again["launched_ids"] == []
    assert again["skipped"] == [
        {
            "member_id": str(chosen),
            "client_profile_id": str(rows[chosen]["client_profile_id"]),
            "full_name": "Ben Ready",
            "code": "already_launched",
            "params": {},
        }
    ]
    assert fake_email_provider.names().count("add_contact") == 1

    # Another campaign's lead, or an unknown one, is never launched from here.
    stranger = (
        await launch_via_api(client, email_world.headers, cid, member_ids=[uuid.uuid4()])
    ).json()
    assert stranger["skipped"][0]["code"] == "not_in_campaign"

    # Launch all eligible: the two others, then nothing left.
    rest = (await launch_via_api(client, email_world.headers, cid)).json()
    assert rest["members_pushed"] == 2 and rest["remaining"] == 0
    assert fake_email_provider.names().count("add_contact") == 3


async def test_a_partial_batch_launches_the_eligible_and_says_why_not_the_others(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    people = ("Ana Lopez", "Ben Odd", "Cleo Demo", "Dan Refused")
    cid, profiles, members = await ready_campaign(
        client, db_session, email_world, make_client_profile, people
    )
    for person, email in (("Ben Odd", "ben@@acme.io"), ("Cleo Demo", DEMO)):
        await db_session.execute(
            update(ClientProfile).where(ClientProfile.id == profiles[person]).values(email=email)
        )
    await db_session.commit()
    # Lemlist refuses one address (its own duplicate rule).
    original = fake_email_provider.add_contact

    def add_contact(ref: CampaignRef, lead: LeadSpec) -> LeadRef:
        if lead.email == "dan@acme.io":
            raise ProviderError(
                "Lead already in another campaign", code="ALREADY_IN_OTHER_CAMPAIGN"
            )
        return original(ref, lead)

    fake_email_provider.add_contact = add_contact  # type: ignore[method-assign]

    result = (await launch_via_api(client, email_world.headers, cid)).json()
    assert result["members_pushed"] == 1
    assert result["launched_ids"] == [str(members["Ana Lopez"])]
    failures = {f["full_name"]: (f["code"], f["params"]) for f in result["failures"]}
    assert failures == {
        "Ben Odd": ("INVALID_EMAIL", {}),
        "Cleo Demo": ("DEMO_RECIPIENT", {}),
        "Dan Refused": ("email.provider_already_in_other_campaign", {}),
    }
    assert result["members_failed"] == 3
    # The two the rules refused never reached Lemlist.
    added = [kw["email"] for name, kw in fake_email_provider.ops if name == "add_contact"]
    assert "ben@@acme.io" not in added and DEMO not in added
    rows = {m["id"]: m for m in await members_of(db_session, cid)}
    for person in ("Ben Odd", "Cleo Demo", "Dan Refused"):
        row = rows[members[person]]
        assert row["status"] == "EMAIL_READY", person
        assert row["status_reason"] == failures[person][0], person
    # Tried again, the refused come last (the launch order): a lead added
    # since goes first.
    fresh = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Eve", last_name="Fresh", email="eve@acme.io"
    )
    await add_via_api(client, email_world.headers, cid, client_profile_ids=[fresh.id])
    readiness = await readiness_of(client, email_world.headers, cid)
    order = [r["lead"]["full_name"] for r in readiness["items"]]
    assert order[0] == "Eve Fresh"
    assert set(order[1:]) == {"Ben Odd", "Cleo Demo", "Dan Refused"}
    last = by_name(readiness)["Dan Refused"]
    assert {
        "code": "LAST_ATTEMPT",
        "params": {"reason": "email.provider_already_in_other_campaign"},
        "blocking": False,
    } in last["issues"]


async def test_a_template_without_its_value_is_refused_before_any_ai_call(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    ai_calls: list[str],
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, members = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Nosite",), activate=False
    )
    third = await step_via_api(
        client,
        email_world.headers,
        cid,
        name="Dernier mot",
        subject_template="",
        body_template="Vu sur {{company.website}}.\n\n{{ai.hook}}",
    )
    step_id = next(s["id"] for s in third["steps"] if s["position"] == 3)
    await client.post(
        f"/email/steps/{step_id}/ai-variables",
        json={"key": "hook", "prompt_template": "Une phrase sur {{client.first_name}}."},
        headers=email_world.headers,
    )
    await set_status(client, email_world.headers, cid, "ACTIVE")

    result = (await launch_via_api(client, email_world.headers, cid)).json()
    assert result["members_pushed"] == 0
    # Refused on its template before any AI value was written for her.
    assert ai_calls == []
    [failure] = result["failures"]
    # She has no company at all: said once, before the value it leaves empty.
    assert (failure["code"], failure["params"]) == (
        "TEMPLATE",
        {"position": 3, "kind": "no_company"},
    )
    assert "add_contact" not in fake_email_provider.names()
    assert members


async def test_launch_needs_an_active_campaign_and_respects_the_daily_limit(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, members = await ready_campaign(
        client, db_session, email_world, make_client_profile, activate=False, daily_limit=1
    )
    refused = await launch_via_api(client, email_world.headers, cid)
    assert refused.status_code == 409
    assert refused.json()["code"] == "email.campaign_not_active"
    assert refused.json()["params"] == {"status": "DRAFT"}
    assert fake_email_provider.ops == []

    await set_status(client, email_world.headers, cid, "ACTIVE")
    selected = [members["Ana Lopez"], members["Ben Ready"]]
    first = (await launch_via_api(client, email_world.headers, cid, member_ids=selected)).json()
    assert first["members_pushed"] == 1 and first["remaining"] == 1
    assert first["notes"] == [
        {"code": "daily_limit_reached", "params": {"launched_today": 1, "daily_limit": 1}}
    ]
    second = (await launch_via_api(client, email_world.headers, cid, member_ids=selected)).json()
    assert second["members_pushed"] == 0
    assert fake_email_provider.names().count("add_contact") == 1


async def test_a_sync_never_launches_and_a_provider_error_launches_nobody(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    cid, _, _ = await ready_campaign(client, db_session, email_world, make_client_profile)
    synced = await sync_via_api(client, email_world.headers, cid)
    assert synced.status_code == 200, synced.text
    body = synced.json()
    assert body["members_pushed"] == 0 and body["remaining"] == 3
    assert body["notes"] == [{"code": "ready_to_launch", "params": {"count": 3}}]
    assert "add_contact" not in fake_email_provider.names()
    assert "launch_contact" not in fake_email_provider.names()

    # Lemlist fails on the campaign itself: nothing is launched.
    def broken_update(*_: Any, **__: Any) -> None:
        raise ProviderError("Lemlist is down", code="RETRY_LATER")

    fake_email_provider.update_campaign = broken_update  # type: ignore[method-assign]
    failed = await launch_via_api(client, email_world.headers, cid)
    assert failed.status_code == 409
    assert failed.json()["code"] == "email.provider_retry_later"
    assert "add_contact" not in fake_email_provider.names()
    sync_error = await db_session.scalar(
        select(EmailCampaign.sync_error)
        .where(EmailCampaign.id == uuid.UUID(cid))
        .execution_options(populate_existing=True)
    )
    assert sync_error == "email.provider_retry_later"


async def test_a_dry_run_launch_simulates_the_hand_over(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
) -> None:
    """Without Lemlist for the agency (the default), a launch reaches no
    one: the leads are queued with dry-run ids, and say so."""
    cid, _, members = await ready_campaign(client, db_session, email_world, make_client_profile)
    result = (await launch_via_api(client, email_world.headers, cid)).json()
    assert result["external"] is False and result["members_pushed"] == 3
    leads = {
        row["id"]: row
        for row in (
            await client.get(
                "/email/members", params={"campaign_id": cid}, headers=email_world.headers
            )
        ).json()["items"]
    }
    for member in members.values():
        lead = leads[str(member)]
        assert lead["dry_run"] is True and lead["status"] == "EMAIL_QUEUED", lead
        assert lead["queued_at"] is not None


# ── Nidria's own gates and Lemlist's refusals ────────────────────────────────


async def test_another_teams_campaign_launches_nobody(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """A campaign Lemlist holds in another team than the key's is read only
    (rule N4): the Launch page says TEAM_MISMATCH, a launch and a sync are
    refused, and nothing reaches the current team."""
    cid, _, _ = await ready_campaign(client, db_session, email_world, make_client_profile)
    await make_live(db_session, cid, team="tea_old", status="ACTIVE")

    readiness = await readiness_of(client, email_world.headers, cid)
    assert readiness["can_launch"] is False
    assert [g["code"] for g in readiness["gates"] if g["blocking"]] == ["TEAM_MISMATCH"]
    for response in (
        await launch_via_api(client, email_world.headers, cid),
        await sync_via_api(client, email_world.headers, cid),
    ):
        assert response.status_code == 409
        assert response.json()["code"] == "email.team_mismatch"
    assert fake_email_provider.ops == []


async def test_a_plan_refusal_stops_the_batch_and_the_next_launch_takes_the_lead_up(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """Lemlist's plan refuses the launch (402): the batch stops there, with
    `email.provider_plan_required`; the lead stays added and personalized,
    and the next Launch launches that very lead, never a second one."""
    cid, _, members = await ready_campaign(client, db_session, email_world, make_client_profile)
    original = fake_email_provider.launch_contact

    def refused(lead: LeadRef) -> None:
        raise ProviderError("Upgrade your plan to launch leads", code="PLAN_REQUIRED", status=402)

    fake_email_provider.launch_contact = refused  # type: ignore[method-assign]
    stopped = (await launch_via_api(client, email_world.headers, cid)).json()
    assert stopped["stopped"] is True and stopped["members_pushed"] == 0
    assert stopped["notes"] == [{"code": "plan_refused", "params": {}}]
    [failure] = stopped["failures"]
    assert failure["code"] == "email.provider_plan_required"
    first = failure["member_id"]
    # Only the first lead was tried: the batch stopped there.
    assert fake_email_provider.names().count("add_contact") == 1
    assert fake_email_provider.names().count("set_step_content") == 2
    rows = {str(m["id"]): m for m in await members_of(db_session, cid)}
    assert rows[first]["status"] == "EMAIL_READY"
    assert rows[first]["status_reason"] == "email.provider_plan_required"
    lead_id = rows[first]["provider_lead_id"]
    assert lead_id is not None

    fake_email_provider.launch_contact = original  # type: ignore[method-assign]
    fake_email_provider.ops.clear()
    resumed = (
        await launch_via_api(client, email_world.headers, cid, member_ids=[uuid.UUID(first)])
    ).json()
    assert resumed["launched_ids"] == [first]
    # The same lead, already personalized: launched, nothing added again.
    # Lemlist holds a lead now: the campaign starts once it is safe, last.
    assert fake_email_provider.names() == [
        "update_campaign",
        "update_step",
        "update_step",
        "sync_campaign",
        "launch_contact",
        "start_campaign",
    ]
    assert ("launch_contact", {"lead": lead_id}) in fake_email_provider.ops
    assert members


async def test_a_step_lemlist_already_reached_keeps_the_email_nidria_holds(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
) -> None:
    """Lemlist answers 409 "already reached" for a step: the render Nidria
    holds stays the email really sent, never overwritten, and the lead is
    not launched (`email.provider_step_reached`)."""
    cid, _, members = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez",)
    )
    original = fake_email_provider.set_step_content
    calls: list[str] = []

    def reached(ref: CampaignRef, lead: LeadRef, step_id: str, content: Any) -> bool:
        calls.append(step_id)
        if len(calls) == 2:
            # Lemlist already sent this lead its second step.
            fake_email_provider.reached.add((lead.lead_id, step_id))
        return original(ref, lead, step_id, content)

    fake_email_provider.set_step_content = reached  # type: ignore[method-assign]
    result = (await launch_via_api(client, email_world.headers, cid)).json()
    [failure] = result["failures"]
    assert (failure["code"], failure["params"]) == (
        "email.provider_step_reached",
        {"position": 2},
    )
    assert "launch_contact" not in fake_email_provider.names()
    renders = (
        await db_session.scalars(
            select(EmailMemberRender.step_position).where(
                EmailMemberRender.campaign_member_id == members["Ana Lopez"]
            )
        )
    ).all()
    # Step 1 was taken and frozen as pushed; step 2's render was never written.
    assert sorted(renders) == [1]


async def test_a_lead_carries_its_address_and_its_name_only(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
) -> None:
    """Decision A8: Lemlist receives the effective address, the first and
    the last name; never the company, a job title or anything else."""
    seen: list[LeadSpec] = []
    original = fake_email_provider.add_contact

    def recording(ref: CampaignRef, lead: LeadSpec) -> LeadRef:
        seen.append(lead)
        return original(ref, lead)

    fake_email_provider.add_contact = recording  # type: ignore[method-assign]
    cid, profiles, _ = await ready_campaign(
        client, db_session, email_world, make_client_profile, ("Ana Lopez",)
    )
    await make_company_link(
        client_profile_id=profiles["Ana Lopez"], agency_id=email_world.agency_id, name="Acme"
    )
    await launch_via_api(client, email_world.headers, cid)
    assert seen == [LeadSpec(email="ana@acme.io", first_name="Ana", last_name="Lopez")]


# ── the add preview: the same rules ──────────────────────────────────────────


async def test_the_add_preview_refuses_a_malformed_a_demo_and_a_known_bounced_address(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
) -> None:
    agency = email_world.agency_id
    odd = await make_client_profile(agency_id=agency, first_name="Odd", email="odd@mail")
    demo = await make_client_profile(agency_id=agency, first_name="Demo", email=DEMO)
    # Another record bounced on this very address (risk R1): held the same.
    earlier = (await campaign_via_api(client, email_world.headers, "Earlier"))["id"]
    first = await make_client_profile(agency_id=agency, first_name="First", email="Same@acme.io")
    await make_email_member(
        campaign_id=uuid.UUID(earlier),
        client_profile_id=first.id,
        agency_id=agency,
        status="EMAIL_BOUNCED",
    )
    second = await make_client_profile(agency_id=agency, first_name="Second", email="same@acme.io")
    fine = await make_client_profile(agency_id=agency, first_name="Fine", email="fine@acme.io")
    cid = (await campaign_via_api(client, email_world.headers))["id"]
    await step_via_api(client, email_world.headers, cid)
    preview = await client.post(
        f"/email/campaigns/{cid}/members/preview-add",
        json={"client_profile_ids": [str(p.id) for p in (odd, demo, second, fine)]},
        headers=email_world.headers,
    )
    assert preview.status_code == 200, preview.text
    rows = {
        r["first_name"]: (r["eligibility"], r["action"], r["via_client_profile_id"])
        for r in preview.json()["rows"]
    }
    assert rows == {
        "Odd": ("INVALID_EMAIL", "SKIP", None),
        "Demo": ("DEMO_RECIPIENT", "SKIP", None),
        "Second": ("BOUNCED", "SKIP", str(first.id)),
        "Fine": ("READY", "ADD", None),
    }
    summary = preview.json()["summary"]
    assert (summary["invalid_email"], summary["demo_recipient"], summary["bounced"]) == (1, 1, 1)
    assert (summary["will_add"], summary["will_skip"]) == (1, 3)
    # The preview wrote nothing.
    assert await members_of(db_session, cid) == []


# ── shared with the reliability tests ────────────────────────────────────────


def count_ops(provider: FakeProvider) -> Callable[[str], int]:
    return lambda name: provider.names().count(name)


async def member_status(db: AsyncSession, member_id: uuid.UUID) -> str | None:
    return await db.scalar(
        select(EmailCampaignMember.status)
        .where(EmailCampaignMember.id == member_id)
        .execution_options(populate_existing=True)
    )
