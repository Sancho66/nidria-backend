"""The Email module's agent API (phase 3A): campaigns, steps, previews, AI
variables, recipients and their bulk actions, sent from and deleted.

Ported from Prism's `tests/test_email_campaigns.py` (reference 629a2b0), the
parts phase 3A serves: the legacy workflow, the add wizard (phase 3B), the
launch and the sync (phase 3B), and the webhook (phase 4) are not here. A
recipient that Prism launched through the API is stood up by the email
plugin's factories (a membership with its lead, renders, messages, events).

No test reaches Lemlist: the default provider is the dry run; the tests of a
real provider's path use `FakeProvider` (the `fake_email_provider` fixture), which
answers like one and records every call.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import (
    EmailCampaign,
    EmailCampaignMember,
    EmailMemberRender,
    EmailProviderEvent,
)
from shared.models.rbac import Role
from src.core.rbac.permissions import Permission
from src.email_campaigns import email_manager
from src.email_campaigns.email_manager import EmailManager, prompt_hash
from src.email_campaigns.providers.base import ProviderError
from tests.email_fakes import FakeProvider
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    EmailWorld,
    MakeCompanyLink,
    MakeEmailEvent,
    MakeEmailMember,
    MakeEmailMessage,
    MakeEmailRender,
    MakeEmailStep,
)
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.rbac_plugin import MakeRole

pytestmark = pytest.mark.usefixtures("rbac_baseline")

READ = (Permission.EMAIL_VIEW, Permission.CASE_VIEW)


# ── helpers (shared with the other Email API test files) ─────────────────────


async def campaign_via_api(
    client: AsyncClient, headers: dict[str, str], name: str = "Founders", **body: Any
) -> dict[str, Any]:
    response = await client.post("/email/campaigns", json={"name": name, **body}, headers=headers)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def step_via_api(
    client: AsyncClient, headers: dict[str, str], campaign_id: str, **fields: Any
) -> dict[str, Any]:
    """Add a step; the campaign's detail comes back."""
    body = {
        "name": "Email",
        "subject_template": "{{client.first_name}}, une question",
        "body_template": "Bonjour {{client.first_name}},\n\nUn mot de {{agency.name}}.",
        **fields,
    }
    response = await client.post(
        f"/email/campaigns/{campaign_id}/steps", json=body, headers=headers
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def set_status(
    client: AsyncClient, headers: dict[str, str], campaign_id: str, status: str
) -> dict[str, Any]:
    response = await client.patch(
        f"/email/campaigns/{campaign_id}", json={"status": status}, headers=headers
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def act(
    client: AsyncClient, headers: dict[str, str], action: str, ids: list[Any], **body: Any
) -> dict[str, Any]:
    """A bulk action: always 200, a row per membership."""
    response = await client.post(
        f"/email/members/{action}", json={"ids": [str(i) for i in ids], **body}, headers=headers
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def members_of(db: AsyncSession, campaign_id: Any) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(
            EmailCampaignMember.id,
            EmailCampaignMember.client_profile_id,
            EmailCampaignMember.status,
            EmailCampaignMember.status_reason,
            EmailCampaignMember.provider_metadata,
            EmailCampaignMember.provider_lead_id,
            EmailCampaignMember.current_step,
            EmailCampaignMember.moved_to_id,
            EmailCampaignMember.removed_at,
            EmailCampaignMember.removed_by_agent_id,
            EmailCampaignMember.paused_by_agent_id,
            EmailCampaignMember.added_by_agent_id,
        )
        .where(EmailCampaignMember.campaign_id == uuid.UUID(str(campaign_id)))
        .order_by(EmailCampaignMember.added_at, EmailCampaignMember.id)
        .execution_options(populate_existing=True)
    )
    return [dict(row._mapping) for row in rows.all()]


async def make_live(
    db: AsyncSession,
    campaign_id: Any,
    provider_id: str = "cam_live",
    team: str | None = "tea_current",
    status: str | None = None,
) -> None:
    """The campaign as a launch left it at Lemlist (phase 3B): a real
    provider id, created in `team`."""
    campaign = await db.get(EmailCampaign, uuid.UUID(str(campaign_id)))
    assert campaign is not None
    campaign.provider_campaign_id = provider_id
    campaign.provider_sequence_id = "seq_live"
    campaign.provider_schedule_id = "skd_live"
    campaign.provider_metadata = {"team_id": team} if team else {}
    if status is not None:
        campaign.status = status
    await db.commit()


async def role_headers(
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    agency_id: uuid.UUID,
    *permissions: Permission,
) -> dict[str, str]:
    """An agent of the agency whose role holds exactly `permissions`."""
    role: Role = await make_role(permissions=list(permissions), agency_id=agency_id)
    agent = await make_agent(agency_id=agency_id, role=role)
    return agent_headers(agent)


# ── campaigns ────────────────────────────────────────────────────────────────


async def test_campaigns_are_created_in_the_agency_and_follow_their_lifecycle(
    client: AsyncClient, email_world: EmailWorld
) -> None:
    headers = email_world.headers
    first = await campaign_via_api(client, headers, "Founders francophones")
    other = await campaign_via_api(client, email_world.other_headers, "Porto 2026")
    assert first["status"] == "DRAFT" and first["provider_mode"] == "NONE"
    assert first["settings"]["timezone"] == "Europe/Paris"
    assert first["settings"]["daily_limit"] == 50 and first["settings"]["sending_days"] == [
        1,
        2,
        3,
        4,
        5,
    ]
    # No lead at the provider yet: the sequence is free.
    assert first["sequence"]["active_sequence"] is False
    assert all(
        first["sequence"][key]
        for key in ("can_add_step", "can_insert_step", "can_reorder_steps", "can_edit_step_content")
    )
    assert first["created_by_agent_id"] == str(email_world.admin_id)
    uuid.UUID(first["id"])  # identifiers are UUIDs

    nameless = await client.post("/email/campaigns", json={}, headers=headers)
    assert nameless.status_code == 422
    # The agency is the token's: a body cannot place a campaign elsewhere.
    elsewhere = await client.post(
        "/email/campaigns",
        json={"name": "x", "agency_id": str(email_world.other_agency_id)},
        headers=headers,
    )
    assert elsewhere.status_code == 201
    listed = (await client.get("/email/campaigns", headers=headers)).json()
    assert {c["name"] for c in listed} == {"Founders francophones", "x"}
    other_listed = (await client.get("/email/campaigns", headers=email_world.other_headers)).json()
    assert [c["id"] for c in other_listed] == [other["id"]]

    url = f"/email/campaigns/{first['id']}"
    bad_window = await client.patch(
        url,
        json={"settings": {"sending_start_time": "18:00", "sending_end_time": "09:00"}},
        headers=headers,
    )
    assert bad_window.status_code == 422
    bad_zone = await client.patch(
        url, json={"settings": {"timezone": "Mars/Base"}}, headers=headers
    )
    assert bad_zone.status_code == 422
    renamed = await client.patch(
        url,
        json={"name": "Founders FR", "settings": {"daily_limit": 30, "sending_days": [3, 1, 2]}},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Founders FR"
    assert renamed.json()["settings"]["daily_limit"] == 30
    assert renamed.json()["settings"]["sending_days"] == [1, 2, 3]

    no_steps = await client.patch(url, json={"status": "ACTIVE"}, headers=headers)
    assert no_steps.status_code == 400
    assert no_steps.json()["code"] == "email.no_steps"
    await step_via_api(client, headers, first["id"])
    assert (await set_status(client, headers, first["id"], "ACTIVE"))["status"] == "ACTIVE"
    back = await client.patch(url, json={"status": "DRAFT"}, headers=headers)
    assert back.status_code == 400
    assert back.json()["code"] == "email.invalid_transition"
    assert back.json()["params"] == {"from": "ACTIVE", "to": "DRAFT"}
    assert (await set_status(client, headers, first["id"], "PAUSED"))["status"] == "PAUSED"

    # Several statuses: the parameter is repeated, never comma-joined.
    paused_or_draft = await client.get(
        "/email/campaigns", params=[("status", "PAUSED"), ("status", "DRAFT")], headers=headers
    )
    assert {c["name"] for c in paused_or_draft.json()} == {"Founders FR", "x"}
    only_active = await client.get("/email/campaigns", params={"status": "ACTIVE"}, headers=headers)
    assert only_active.json() == []

    # A copy is named by the frontend (decision A13), else keeps the name.
    copy = await client.post(
        f"{url}/duplicate", json={"name": "Founders FR (copie)"}, headers=headers
    )
    assert copy.status_code == 201
    assert copy.json()["name"] == "Founders FR (copie)" and copy.json()["status"] == "DRAFT"
    assert len(copy.json()["steps"]) == 1 and copy.json()["members_count"] == 0
    plain = await client.post(f"{url}/duplicate", headers=headers)
    assert plain.status_code == 201 and plain.json()["name"] == "Founders FR"

    detail = (await client.get(url, headers=headers)).json()
    assert detail["id"] == first["id"] and detail["team_mismatch"] is False


async def test_steps_keep_one_order(client: AsyncClient, email_world: EmailWorld) -> None:
    headers = email_world.headers
    cid = (await campaign_via_api(client, headers))["id"]
    first = await step_via_api(client, headers, cid, name="Email initial")
    second = await step_via_api(client, headers, cid, name="Relance 1", subject_template="")
    assert [s["delay_value"] for s in second["steps"]] == [0, 3]  # day 0, then the default
    inserted = await step_via_api(
        client, headers, cid, name="Relance 0", position=2, delay_value=2, delay_unit="WEEKS"
    )
    assert [s["name"] for s in inserted["steps"]] == ["Email initial", "Relance 0", "Relance 1"]
    assert [s["position"] for s in inserted["steps"]] == [1, 2, 3]
    assert inserted["steps"][1]["delay_unit"] == "WEEKS"

    listed = (await client.get(f"/email/campaigns/{cid}/steps", headers=headers)).json()
    assert [s["id"] for s in listed] == [s["id"] for s in inserted["steps"]]
    assert listed[0]["campaign_id"] == cid and listed[0]["variables_count"] == 2

    ids = [s["id"] for s in inserted["steps"]]
    wrong = await client.post(
        f"/email/campaigns/{cid}/steps/reorder", json={"step_ids": ids[:2]}, headers=headers
    )
    assert wrong.status_code == 400 and wrong.json()["code"] == "email.invalid_step_order"
    twice = await client.post(
        f"/email/campaigns/{cid}/steps/reorder",
        json={"step_ids": [ids[0], ids[0], ids[1]]},
        headers=headers,
    )
    assert twice.status_code == 400
    reordered = await client.post(
        f"/email/campaigns/{cid}/steps/reorder",
        json={"step_ids": [ids[1], ids[0], ids[2]]},
        headers=headers,
    )
    assert reordered.status_code == 200, reordered.text
    assert [s["name"] for s in reordered.json()["steps"]] == [
        "Relance 0",
        "Email initial",
        "Relance 1",
    ]

    duplicated = await client.post(f"/email/steps/{ids[0]}/duplicate", headers=headers)
    assert duplicated.status_code == 201
    assert [s["name"] for s in duplicated.json()["steps"]] == [
        "Relance 0",
        "Email initial",
        "Email initial",
        "Relance 1",
    ]
    named = await client.post(
        f"/email/steps/{ids[2]}/duplicate", json={"name": "Relance 1 bis"}, headers=headers
    )
    assert [s["name"] for s in named.json()["steps"]][-1] == "Relance 1 bis"
    deleted = await client.delete(f"/email/steps/{ids[2]}", headers=headers)
    assert deleted.status_code == 200
    assert [s["position"] for s in deleted.json()["steps"]] == [1, 2, 3, 4]

    step_id = first["steps"][0]["id"]
    before = next(s for s in deleted.json()["steps"] if s["id"] == step_id)
    edited = await client.patch(
        f"/email/steps/{step_id}", json={"body_template": "Nouveau texte"}, headers=headers
    )
    assert edited.status_code == 200
    assert edited.json()["version"] == before["version"] + 1
    assert edited.json()["content_hash"] != before["content_hash"]
    assert edited.json()["unsynced"] is True
    same = await client.patch(f"/email/steps/{step_id}", json={"name": "Renamed"}, headers=headers)
    assert same.json()["version"] == edited.json()["version"]  # the content did not change


async def test_the_first_email_needs_a_subject(
    client: AsyncClient, email_world: EmailWorld
) -> None:
    headers = email_world.headers
    cid = (await campaign_via_api(client, headers))["id"]
    blank = await client.post(
        f"/email/campaigns/{cid}/steps",
        json={"name": "First", "subject_template": "  ", "body_template": "Hi"},
        headers=headers,
    )
    assert blank.status_code == 400
    assert blank.json()["code"] == "email.first_step_subject_required"
    detail = await step_via_api(client, headers, cid, name="First")
    # A follow-up without a subject answers in the thread: allowed.
    detail = await step_via_api(client, headers, cid, name="Follow-up", subject_template="")
    first_id, follow_id = (s["id"] for s in detail["steps"])

    emptied = await client.patch(
        f"/email/steps/{first_id}", json={"subject_template": ""}, headers=headers
    )
    assert emptied.status_code == 400
    assert emptied.json()["code"] == "email.first_step_subject_required"
    swapped = await client.post(
        f"/email/campaigns/{cid}/steps/reorder",
        json={"step_ids": [follow_id, first_id]},
        headers=headers,
    )
    assert swapped.status_code == 400
    dropped = await client.delete(f"/email/steps/{first_id}", headers=headers)
    assert dropped.status_code == 400
    after = (await client.get(f"/email/campaigns/{cid}", headers=headers)).json()
    assert [s["id"] for s in after["steps"]] == [first_id, follow_id]
    assert after["steps"][0]["subject_template"] != ""


# ── preview and AI variables ─────────────────────────────────────────────────


async def test_preview_renders_a_real_client_and_shows_every_error(
    client: AsyncClient,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
    make_expat_user: MakeExpatUser,
) -> None:
    headers = email_world.headers
    maria = await make_client_profile(
        agency_id=email_world.agency_id,
        first_name="Maria",
        last_name="Bras Goncalves",
        email="maria@acme.io",
    )
    await make_company_link(
        client_profile_id=maria.id,
        agency_id=email_world.agency_id,
        name="Acme Lisboa",
        custom_fields={"industry": "Fintech"},
    )
    # Rule N2: a record linked to a client-space account reads the account's.
    account = await make_expat_user(first_name="Joao", last_name="Silva", email="joao@silva.pt")
    joao = await make_client_profile(
        agency_id=email_world.agency_id,
        expat_user_id=account.id,
        first_name="Old",
        last_name="Name",
        email="old@record.pt",
    )
    nomail = await make_client_profile(agency_id=email_world.agency_id, email=None)
    stranger = await make_client_profile(agency_id=email_world.other_agency_id, email="j@dupont.fr")

    async def preview(profile_id: Any, **body: Any) -> Any:
        return await client.post(
            "/email/templates/preview",
            json={"client_profile_id": str(profile_id), **body},
            headers=headers,
        )

    ok = (
        await preview(
            maria.id,
            subject_template="{{client.first_name}}, une question sur {{company.name}}",
            body_template="Bonjour {{client.full_name}}, secteur {{company.industry}}.",
            position=1,
        )
    ).json()
    assert ok["subject"] == "Maria, une question sur Acme Lisboa"
    assert ok["body"] == "Bonjour Maria Bras Goncalves, secteur Fintech."
    assert ok["sendable"] is True and ok["errors"] == []
    assert {v["token"]: v["value"] for v in ok["variables"]}["client.first_name"] == "Maria"
    assert ok["client"]["company_name"] == "Acme Lisboa"
    assert ok["client"]["email"] == "maria@acme.io"

    linked = (
        await preview(joao.id, subject_template="Hi", body_template="{{client.email}}")
    ).json()
    assert linked["body"] == "joao@silva.pt"
    assert linked["client"]["full_name"] == "Joao Silva"

    missing = (
        await preview(maria.id, subject_template="Hi", body_template="{{company.revenue}} {{x}}")
    ).json()
    assert missing["sendable"] is False
    assert {e["kind"] for e in missing["errors"]} == {"unknown_variable", "malformed_token"}
    assert "{{company.revenue}}" in missing["body"]

    no_company = (
        await preview(joao.id, subject_template="Hi", body_template="{{company.name}}")
    ).json()
    assert {e["kind"] for e in no_company["errors"]} == {"no_company", "missing_variable"}

    no_mail = (await preview(nomail.id, subject_template="", body_template="", position=1)).json()
    assert {e["kind"] for e in no_mail["errors"]} == {
        "no_email",
        "first_subject_required",
        "empty_body",
    }

    elsewhere = await preview(stranger.id, subject_template="Hi", body_template="Hi")
    assert elsewhere.status_code == 404
    assert elsewhere.json()["code"] == "profile.not_found"


async def test_a_fallback_keeps_a_client_sendable_and_says_so(
    client: AsyncClient, email_world: EmailWorld, make_client_profile: MakeClientProfile
) -> None:
    maria = await make_client_profile(agency_id=email_world.agency_id, profession=None)
    response = await client.post(
        "/email/templates/preview",
        json={
            "client_profile_id": str(maria.id),
            "subject_template": "Hi",
            "body_template": 'Votre métier : {{client.profession | "votre activité"}}.',
        },
        headers=email_world.headers,
    )
    preview = response.json()
    assert preview["sendable"] is True, preview["errors"]
    assert preview["body"] == "Votre métier : votre activité."
    [used] = [v for v in preview["variables"] if v["token"] == "client.profession"]
    assert (used["value"], used["fallback"]) == (None, "votre activité")


async def test_variable_fill_counts_the_campaign_recipients_else_the_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
    make_email_member: MakeEmailMember,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    email_manager.clear_caches()
    headers = email_world.headers
    ana = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Ana", email="ana@acme.io", phone="+351 1"
    )
    await make_company_link(client_profile_id=ana.id, agency_id=email_world.agency_id)
    ben = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Ben", email="ben@b.io", phone=None
    )
    ben_id = ben.id
    # Without an email a client is never emailed: not counted.
    await make_client_profile(agency_id=email_world.agency_id, email=None)
    # Another agency's client never counts here.
    await make_client_profile(agency_id=email_world.other_agency_id, email="z@z.io")

    async def fill(**params: Any) -> dict[str, Any]:
        response = await client.get("/email/variables/fill", params=params, headers=headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    agency = await fill()
    assert (agency["population"], agency["total"], agency["considered"]) == ("agency", 2, 2)
    assert agency["campaign_id"] is None
    assert agency["filled"]["client.first_name"] == 2
    assert agency["filled"]["client.phone"] == 1
    assert agency["filled"]["company.name"] == 1
    assert agency["filled"]["agency.name"] == 2
    # Cached ten minutes per agency, campaign and population size: a value
    # changed meanwhile is not read again.
    await db_session.execute(
        text("UPDATE client_profile SET phone = '+351 2' WHERE id = :id"), {"id": ben_id}
    )
    await db_session.commit()
    assert (await fill())["filled"]["client.phone"] == 1
    # Beyond the cap, a random sample is resolved (the cap is 2000).
    email_manager.clear_caches()
    monkeypatch.setattr(email_manager, "FILL_SAMPLE", 1)
    sampled = await fill()
    assert (sampled["total"], sampled["considered"]) == (2, 1)
    assert sampled["filled"]["client.first_name"] == 1  # counted on the sample only
    monkeypatch.setattr(email_manager, "FILL_SAMPLE", 2000)
    email_manager.clear_caches()

    # A campaign without recipients reads the agency; with some, only them.
    cid = (await campaign_via_api(client, headers))["id"]
    assert (await fill(campaign_id=cid))["population"] == "agency"
    await make_email_member(
        campaign_id=uuid.UUID(cid), client_profile_id=ben_id, agency_id=email_world.agency_id
    )
    campaign = await fill(campaign_id=cid)
    assert (campaign["population"], campaign["total"], campaign["campaign_id"]) == (
        "campaign",
        1,
        cid,
    )
    assert campaign["filled"]["client.phone"] == 1  # read after the cache was dropped
    assert campaign["filled"]["client.first_name"] == 1

    # Another agency's campaign does not exist here.
    other_cid = (await campaign_via_api(client, email_world.other_headers))["id"]
    other = await client.get(
        "/email/variables/fill", params={"campaign_id": other_cid}, headers=headers
    )
    assert other.status_code == 404 and other.json()["code"] == "email.campaign_not_found"
    # Reading the module is not managing it.
    reader = await role_headers(make_role, make_agent, agent_headers, email_world.agency_id, *READ)
    refused = await client.get("/email/variables/fill", headers=reader)
    assert refused.status_code == 403
    assert refused.json()["params"] == {"permission": "email.manage"}


async def test_ai_variables_are_checked_and_never_invented(
    client: AsyncClient, email_world: EmailWorld, make_client_profile: MakeClientProfile
) -> None:
    headers = email_world.headers
    maria = await make_client_profile(agency_id=email_world.agency_id, first_name="Maria")
    cid = (await campaign_via_api(client, headers))["id"]
    step = (await step_via_api(client, headers, cid))["steps"][0]
    url = f"/email/steps/{step['id']}/ai-variables"
    prompt = 'Métier : {{client.profession | "inconnu"}}. Une phrase, pas d\'invention.'

    bad_key = await client.post(
        url, json={"key": "Company-Context", "prompt_template": prompt}, headers=headers
    )
    assert bad_key.status_code == 400
    assert bad_key.json()["code"] == "email.ai_variable_key_invalid"
    assert bad_key.json()["params"] == {"key": "Company-Context"}
    nested = await client.post(
        url, json={"key": "a", "prompt_template": "{{ai.b}}"}, headers=headers
    )
    assert nested.status_code == 400
    assert nested.json()["code"] == "email.ai_prompt_invalid"
    assert nested.json()["params"] == {"token": "ai.b"}
    created = await client.post(
        url, json={"key": "context", "prompt_template": prompt}, headers=headers
    )
    assert created.status_code == 201
    assert created.json()["ai_variables"][0]["key"] == "context"
    assert created.json()["version"] == step["version"] + 1  # a prompt is content
    variable_id = created.json()["ai_variables"][0]["id"]
    again = await client.post(
        url, json={"key": "context", "prompt_template": prompt}, headers=headers
    )
    assert again.status_code == 409 and again.json()["code"] == "email.ai_variable_exists"
    updated = await client.patch(
        f"/email/ai-variables/{variable_id}",
        json={"key": "hook", "prompt_template": "Une accroche."},
        headers=headers,
    )
    assert updated.status_code == 200
    assert [(v["key"], v["prompt_template"]) for v in updated.json()["ai_variables"]] == [
        ("hook", "Une accroche.")
    ]

    # Decision A14: the test and the generation answer 409 until D3 is decided.
    tested = await client.post(
        "/email/ai-variables/test",
        json={"client_profile_id": str(maria.id), "key": "hook", "prompt_template": prompt},
        headers=headers,
    )
    assert tested.status_code == 409 and tested.json()["code"] == "email.ai_not_configured"

    body = {
        "client_profile_id": str(maria.id),
        "subject_template": "Bonjour",
        "body_template": "{{ai.hook}}",
        "ai_variables": [{"key": "hook", "prompt_template": "Une accroche."}],
    }
    pending = (await client.post("/email/templates/preview", json=body, headers=headers)).json()
    assert pending["errors"] == [{"kind": "ai_not_generated", "token": "ai.hook"}]
    assert pending["sendable"] is False and "{{ai.hook}}" in pending["body"]
    generate = await client.post(
        "/email/templates/preview", json={**body, "generate_ai": True}, headers=headers
    )
    assert generate.status_code == 409 and generate.json()["code"] == "email.ai_not_configured"
    # A value this editor got earlier is shown again, never stored.
    echoed = (
        await client.post(
            "/email/templates/preview",
            json={**body, "ai_values": {"hook": "Une accroche écrite."}},
            headers=headers,
        )
    ).json()
    assert echoed["sendable"] is True and echoed["body"] == "Une accroche écrite."
    # An AI variable takes no fallback: never replaced by a default text.
    fallback = (
        await client.post(
            "/email/templates/preview",
            json={**body, "body_template": '{{ai.hook | "Bonjour"}}'},
            headers=headers,
        )
    ).json()
    assert [e["kind"] for e in fallback["errors"]] == ["malformed_token"]

    deleted = await client.delete(f"/email/ai-variables/{variable_id}", headers=headers)
    assert deleted.status_code == 200 and deleted.json()["ai_variables"] == []


async def test_a_preview_reads_and_freezes_the_ai_values_the_email_will_carry(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
    make_email_render: MakeEmailRender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = email_world.headers
    ana = await make_client_profile(agency_id=email_world.agency_id, first_name="Ana")
    cid = (await campaign_via_api(client, headers))["id"]
    step = (await step_via_api(client, headers, cid, body_template="{{ai.hook}}"))["steps"][0]
    prompt = "Une accroche pour {{client.first_name}}."
    saved = (
        await client.post(
            f"/email/steps/{step['id']}/ai-variables",
            json={"key": "hook", "prompt_template": prompt},
            headers=headers,
        )
    ).json()
    member = await make_email_member(
        campaign_id=uuid.UUID(cid), client_profile_id=ana.id, agency_id=email_world.agency_id
    )
    member_id = member.id
    body = {
        "client_profile_id": str(ana.id),
        "step_id": saved["id"],
        "subject_template": saved["subject_template"],
        "body_template": saved["body_template"],
        "ai_variables": [{"key": "hook", "prompt_template": prompt}],
    }

    # Frozen earlier, for this very prompt: the preview shows it, sendable.
    await make_email_render(
        campaign_member_id=member_id,
        campaign_step_id=uuid.UUID(saved["id"]),
        step_position=1,
        subject=saved["subject_template"],
        body="Accroche figée",
        ai_values={"hook": {"value": "Accroche figée", "prompt_hash": prompt_hash(prompt, None)}},
        template_version=saved["version"],
        template_hash=saved["content_hash"],
    )
    frozen = (await client.post("/email/templates/preview", json=body, headers=headers)).json()
    assert frozen["body"] == "Accroche figée" and frozen["sendable"] is True
    # Another prompt is another value: the frozen one is not used for it.
    changed = (
        await client.post(
            "/email/templates/preview",
            json={**body, "ai_variables": [{"key": "hook", "prompt_template": "Autre."}]},
            headers=headers,
        )
    ).json()
    assert changed["errors"] == [{"kind": "ai_not_generated", "token": "ai.hook"}]

    # Phase 5's seam: a generation, here stood in for, freezes what it wrote
    # in the recipient's render of the step.
    calls: list[str] = []

    async def generated(
        self: EmailManager, access: Any, profile: Any, people: Any, variables: Any
    ) -> tuple[dict[str, str], dict[str, str]]:
        calls.extend(key for key, _, _ in variables)
        return {key: f"Accroche n°{len(calls)}" for key, _, _ in variables}, {}

    monkeypatch.setattr(EmailManager, "generate_ai_values", generated)
    await db_session.execute(
        text("DELETE FROM email_member_render WHERE campaign_member_id = :m"), {"m": member_id}
    )
    await db_session.commit()
    preview = (
        await client.post(
            "/email/templates/preview", json={**body, "generate_ai": True}, headers=headers
        )
    ).json()
    assert preview["body"] == "Accroche n°1" and calls == ["hook"]
    render = await db_session.scalar(
        select(EmailMemberRender)
        .where(EmailMemberRender.campaign_member_id == member_id)
        .execution_options(populate_existing=True)
    )
    assert render is not None and render.body == "Accroche n°1"
    assert render.ai_values == {
        "hook": {"value": "Accroche n°1", "prompt_hash": prompt_hash(prompt, None)}
    }
    # Previewed again: the frozen value, no new generation.
    again = (
        await client.post(
            "/email/templates/preview", json={**body, "generate_ai": True}, headers=headers
        )
    ).json()
    assert again["body"] == "Accroche n°1" and calls == ["hook"]


# ── recipients ───────────────────────────────────────────────────────────────


async def test_removing_an_ended_lead_takes_it_out_of_lemlist_too(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
) -> None:
    """Lemlist refuses an address that is still a lead of another campaign,
    even an ended one: Remove takes a replied lead out of Lemlist as well, so
    the person can join another campaign later. Removed once, never twice."""
    cid = (await campaign_via_api(client, email_world.headers, "Ended"))["id"]
    await make_live(db_session, cid)
    rita = await make_client_profile(agency_id=email_world.agency_id, first_name="Rita")
    member = await make_email_member(
        campaign_id=uuid.UUID(cid),
        client_profile_id=rita.id,
        status="EMAIL_REPLIED",
        provider_lead_id="lea_rita",
        queued_at=datetime.now(UTC) - timedelta(days=2),
        current_step=1,
    )
    member_id = member.id
    for _ in range(2):
        removed = await act(client, email_world.headers, "remove", [member_id])
        assert removed["rows"][0]["ok"] is True, removed
        assert removed["rows"][0]["status"] == "EMAIL_REMOVED"
    assert fake_email_provider.names().count("remove_contact") == 1
    assert ("remove_contact", {"lead": "lea_rita"}) in fake_email_provider.ops
    [row] = await members_of(db_session, cid)
    assert (row["status_reason"], row["removed_by_agent_id"]) == ("removed", email_world.admin_id)


async def test_pause_resume_remove_and_move(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
    make_email_step: MakeEmailStep,
) -> None:
    headers = email_world.headers
    source = (await campaign_via_api(client, headers, "Source"))["id"]
    target = (await campaign_via_api(client, headers, "Target"))["id"]
    other = (await campaign_via_api(client, email_world.other_headers, "Porto"))["id"]
    await make_live(db_session, source)
    await make_email_step(campaign_id=uuid.UUID(source), provider_step_id="stp_1")
    ids = []
    for index in range(3):
        person = await make_client_profile(
            agency_id=email_world.agency_id, first_name=f"P{index}", email=f"p{index}@x.io"
        )
        member = await make_email_member(
            campaign_id=uuid.UUID(source),
            client_profile_id=person.id,
            status="EMAIL_QUEUED",
            provider_lead_id=f"lea_{index}",
            queued_at=datetime.now(UTC) - timedelta(hours=1),
        )
        ids.append(member.id)

    paused = await act(client, headers, "pause", ids[:2])
    assert paused["done"] == 2 and fake_email_provider.names().count("pause_contact") == 2
    rows = {m["id"]: m for m in await members_of(db_session, source)}
    assert {rows[i]["paused_by_agent_id"] for i in ids[:2]} == {email_world.admin_id}
    again = await act(client, headers, "pause", ids[:1])
    assert again["rows"][0]["ok"] is True  # already paused: nothing to do
    assert fake_email_provider.names().count("pause_contact") == 2
    # Its email for every step is at Lemlist (stood up here): it resumes.
    db_session.add(
        EmailMemberRender(
            campaign_member_id=ids[0],
            campaign_step_id=(await _step_ids(db_session, source))[0],
            step_position=1,
            subject="s",
            body="b",
            template_version=1,
            template_hash="h",
            provider_synced_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    resumed = await act(client, headers, "resume", ids[:1])
    assert resumed["rows"][0] == {
        "id": str(ids[0]),
        "ok": True,
        "status": "EMAIL_QUEUED",
        "code": None,
        "params": {},
        "new_id": None,
    }
    assert ("resume_contact", {"lead": "lea_0"}) in fake_email_provider.ops
    rows = {m["id"]: m for m in await members_of(db_session, source)}
    assert rows[ids[0]]["paused_by_agent_id"] is None

    removed = await act(client, headers, "remove", ids[2:])
    assert removed["rows"][0]["status"] == "EMAIL_REMOVED"
    assert fake_email_provider.names().count("remove_contact") == 1

    # Never to another agency's campaign: it does not exist here.
    cross = await client.post(
        "/email/members/move",
        json={"ids": [str(ids[0])], "target_campaign_id": other},
        headers=headers,
    )
    assert cross.status_code == 404 and cross.json()["code"] == "email.campaign_not_found"
    # From the other agency, these memberships do not exist.
    through_other = await act(
        client, email_world.other_headers, "move", ids[:1], target_campaign_id=other
    )
    assert through_other["failed"] == 1
    assert through_other["rows"][0]["code"] == "email.not_found"
    assert await members_of(db_session, other) == []
    same = await act(client, headers, "move", ids[:1], target_campaign_id=source)
    assert same["rows"][0]["code"] == "email.same_campaign"

    moved = await act(client, headers, "move", ids[:2], target_campaign_id=target)
    assert moved["done"] == 2, moved
    old = {m["id"]: m for m in await members_of(db_session, source)}
    new = await members_of(db_session, target)
    assert {old[i]["status"] for i in ids[:2]} == {"EMAIL_REMOVED"}
    assert {old[i]["moved_to_id"] for i in ids[:2]} == {m["id"] for m in new}
    assert {str(r["new_id"]) for r in moved["rows"]} == {str(m["id"]) for m in new}
    assert {old[i]["status_reason"] for i in ids[:2]} == {"moved"}
    assert old[ids[0]]["provider_metadata"]["status_reason_params"] == {
        "campaign_id": target,
        "campaign_name": "Target",
    }
    assert {old[i]["removed_by_agent_id"] for i in ids[:2]} == {email_world.admin_id}
    # Nothing carried over: the new memberships start from scratch.
    assert {(m["status"], m["provider_lead_id"], m["current_step"]) for m in new} == {
        ("EMAIL_READY", None, None)
    }
    assert {m["added_by_agent_id"] for m in new} == {email_world.admin_id}
    # Never twice in a campaign: the person already holds an open membership there.
    twice = await act(client, headers, "move", ids[:1], target_campaign_id=target)
    assert twice["rows"][0]["code"] == "email.already_in_campaign"
    # A removed membership moves (it is not live): a new, ready one opens.
    revived = await act(client, headers, "move", ids[2:], target_campaign_id=target)
    assert revived["rows"][0]["ok"] is True and revived["rows"][0]["new_id"]

    listed = (
        await client.get(
            "/email/members",
            params={"campaign_id": source, "include_removed": "true"},
            headers=headers,
        )
    ).json()
    assert listed["total"] == 3 and listed["status_counts"] == {"EMAIL_REMOVED": 3}
    shown = {item["id"]: item for item in listed["items"]}
    assert shown[str(ids[0])]["status_reason"] == "moved"
    assert shown[str(ids[0])]["status_reason_params"]["campaign_name"] == "Target"
    default = await client.get("/email/members", params={"campaign_id": source}, headers=headers)
    assert default.json()["total"] == 0  # ended memberships are hidden by default
    detail = (await client.get(f"/email/members/{ids[0]}", headers=headers)).json()
    assert detail["campaign_name"] == "Source" and len(detail["steps"]) == 1
    assert detail["steps"][0]["provider_synced_at"] is not None
    assert detail["steps"][0]["status"] == "QUEUED"


async def _step_ids(db: AsyncSession, campaign_id: Any) -> list[uuid.UUID]:
    rows = await db.execute(
        text("SELECT id FROM email_campaign_step WHERE campaign_id = :c ORDER BY position"),
        {"c": uuid.UUID(str(campaign_id))},
    )
    return [row[0] for row in rows.all()]


async def test_a_removal_keeps_an_unsubscribe_and_its_campaign_can_be_deleted(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
) -> None:
    """Decision A1 (rule N6): a removal ends the membership at Lemlist too
    but keeps its status, and the person is never moved elsewhere. Since the
    opposition list (08/10/2026), the campaign holding it is deleted like any
    other (the list keeps the address: `test_email_suppression`)."""
    headers = email_world.headers
    cid = (await campaign_via_api(client, headers, "Blocked"))["id"]
    await make_live(db_session, cid)
    gone = await make_client_profile(agency_id=email_world.agency_id, first_name="Gone")
    member = await make_email_member(
        campaign_id=uuid.UUID(cid),
        client_profile_id=gone.id,
        status="EMAIL_UNSUBSCRIBED",
        provider_lead_id="lea_gone",
        queued_at=datetime.now(UTC) - timedelta(days=1),
    )
    removed = await act(client, headers, "remove", [member.id])
    assert removed["rows"][0]["ok"] is True
    assert removed["rows"][0]["status"] == "EMAIL_UNSUBSCRIBED"
    assert ("remove_contact", {"lead": "lea_gone"}) in fake_email_provider.ops
    [row] = await members_of(db_session, cid)
    assert row["status"] == "EMAIL_UNSUBSCRIBED" and row["removed_at"] is not None
    # Removed, the unsubscribe still blocks the person (readiness reads it).
    moved = await act(
        client,
        headers,
        "move",
        [member.id],
        target_campaign_id=(await campaign_via_api(client, headers, "Elsewhere"))["id"],
    )
    assert moved["rows"][0]["code"] == "email.member_blocked"
    assert moved["rows"][0]["params"] == {"status": "EMAIL_UNSUBSCRIBED"}

    deleted = await client.delete(f"/email/campaigns/{cid}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert (deleted.json()["provider_paused"], deleted.json()["provider_kept"]) == (True, True)
    assert ("pause_campaign", {"campaign": "cam_live"}) in fake_email_provider.ops
    assert await members_of(db_session, cid) == []


async def test_moving_an_ended_membership_of_someone_live_elsewhere_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
) -> None:
    """Decision A11: one live membership per person, never two. Prism let the
    database refuse it (a 500); the row says why instead."""
    headers = email_world.headers
    first = (await campaign_via_api(client, headers, "First"))["id"]
    running = (await campaign_via_api(client, headers, "Running"))["id"]
    target = (await campaign_via_api(client, headers, "Target"))["id"]
    lea = await make_client_profile(agency_id=email_world.agency_id, first_name="Lea")
    ended = await make_email_member(
        campaign_id=uuid.UUID(first),
        client_profile_id=lea.id,
        status="EMAIL_COMPLETED",
        removed_at=None,
    )
    await make_email_member(
        campaign_id=uuid.UUID(running), client_profile_id=lea.id, status="EMAIL_READY"
    )
    result = await act(client, headers, "move", [ended.id], target_campaign_id=target)
    assert result["rows"][0]["ok"] is False
    assert result["rows"][0]["code"] == "email.in_other_campaign"
    assert result["rows"][0]["params"] == {"campaign_id": running}
    assert await members_of(db_session, target) == []


async def test_lists_filter_the_recipients_and_say_what_holds_each_back(
    client: AsyncClient,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_company_link: MakeCompanyLink,
    make_email_member: MakeEmailMember,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    headers = email_world.headers
    cid = (await campaign_via_api(client, headers, "Leads"))["id"]
    other_cid = (await campaign_via_api(client, headers, "Other"))["id"]
    ana = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Ana", last_name="Lopez", email="ana@acme.io"
    )
    await make_company_link(
        client_profile_id=ana.id, agency_id=email_world.agency_id, name="Lisboa Ventures"
    )
    account = await make_expat_user(first_name="Bruno", last_name="Costa", email="bruno@costa.pt")
    bruno = await make_client_profile(
        agency_id=email_world.agency_id, expat_user_id=account.id, email=None
    )
    await make_client_case(agency_id=email_world.agency_id, principal_expat_user_id=account.id)
    nomail = await make_client_profile(
        agency_id=email_world.agency_id, first_name="No", last_name="Mail", email=None
    )
    demo = await make_client_profile(
        agency_id=email_world.agency_id, first_name="Demo", email="demo+lisboa-1@nidria.app"
    )
    now = datetime.now(UTC)
    m_ana = await make_email_member(
        campaign_id=uuid.UUID(cid),
        client_profile_id=ana.id,
        status="EMAIL_ACTIVE",
        provider_lead_id="dry_lea_ana",
        queued_at=now - timedelta(days=3),
        current_step=1,
        added_at=now - timedelta(days=5),
    )
    m_bruno = await make_email_member(
        campaign_id=uuid.UUID(cid), client_profile_id=bruno.id, added_at=now - timedelta(days=1)
    )
    m_nomail = await make_email_member(campaign_id=uuid.UUID(cid), client_profile_id=nomail.id)
    m_demo = await make_email_member(campaign_id=uuid.UUID(other_cid), client_profile_id=demo.id)

    async def listed(*params: tuple[str, str]) -> dict[str, Any]:
        response = await client.get("/email/members", params=list(params), headers=headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    everything = await listed()
    assert everything["total"] == 4
    by_id = {item["id"]: item for item in everything["items"]}
    ana_row = by_id[str(m_ana.id)]
    assert (ana_row["full_name"], ana_row["company_name"]) == ("Ana Lopez", "Lisboa Ventures")
    assert ana_row["client_status"] == "prospect" and ana_row["dry_run"] is True
    assert ana_row["issues"] == []  # launched: nothing to say before a launch
    assert "provider_url" not in ana_row  # decision A9: no link to Lemlist's app
    bruno_row = by_id[str(m_bruno.id)]
    # Rule N2: the account's identity; a live case makes a client.
    assert (bruno_row["email"], bruno_row["full_name"]) == ("bruno@costa.pt", "Bruno Costa")
    assert bruno_row["client_status"] == "client"
    assert bruno_row["issues"] == []
    assert [i["code"] for i in by_id[str(m_nomail.id)]["issues"]] == ["NO_EMAIL"]
    assert [i["code"] for i in by_id[str(m_demo.id)]["issues"]] == ["DEMO_RECIPIENT"]
    assert by_id[str(m_demo.id)]["issues"][0]["blocking"] is True

    assert {i["id"] for i in (await listed(("campaign_id", cid)))["items"]} == {
        str(m_ana.id),
        str(m_bruno.id),
        str(m_nomail.id),
    }
    statuses = await listed(("status", "EMAIL_ACTIVE"), ("status", "EMAIL_PAUSED"))
    assert [i["id"] for i in statuses["items"]] == [str(m_ana.id)]
    assert statuses["status_counts"] == {"EMAIL_ACTIVE": 1, "EMAIL_READY": 3}
    clients = await listed(("client_status", "client"))
    assert [i["id"] for i in clients["items"]] == [str(m_bruno.id)]
    prospects = await listed(("client_status", "prospect"))
    assert len(prospects["items"]) == 3
    both = await listed(("client_status", "prospect"), ("client_status", "client"))
    assert both["total"] == 4
    assert {i["id"] for i in (await listed(("has_email", "false")))["items"]} == {str(m_nomail.id)}
    assert [i["id"] for i in (await listed(("launched", "true")))["items"]] == [str(m_ana.id)]
    assert (await listed(("launched", "false")))["total"] == 3
    assert [i["id"] for i in (await listed(("step", "1")))["items"]] == [str(m_ana.id)]
    for term in ("lopez", "acme.io", "ventures", "Ana Lop", "costa.pt"):
        found = {i["id"] for i in (await listed(("search", term)))["items"]}
        assert found <= {str(m_ana.id), str(m_bruno.id)} and found, term
    since = (now - timedelta(days=2)).isoformat()
    assert str(m_ana.id) not in {i["id"] for i in (await listed(("added_after", since)))["items"]}
    paged = await listed(("page", "2"), ("page_size", "3"))
    assert (paged["total"], len(paged["items"]), paged["page"]) == (4, 1, 2)
    bad = await client.get("/email/members", params={"client_status": "vip"}, headers=headers)
    assert bad.status_code == 422


async def test_a_recipients_detail_shows_its_journey_conversation_and_hold(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
    make_email_step: MakeEmailStep,
    make_email_render: MakeEmailRender,
    make_email_message: MakeEmailMessage,
    make_email_event: MakeEmailEvent,
) -> None:
    headers = email_world.headers
    cid = (await campaign_via_api(client, headers, "Journey"))["id"]
    await make_live(db_session, cid, status="ACTIVE")
    s1 = await make_email_step(
        campaign_id=uuid.UUID(cid), position=1, provider_step_id="stp_1", delay_value=0
    )
    s2 = await make_email_step(
        campaign_id=uuid.UUID(cid),
        position=2,
        name="Follow-up",
        subject_template="",
        body_template="Relance.",
        provider_step_id="stp_2",
        delay_value=3,
    )
    s1_id, s2_id = s1.id, s2.id
    sent_at = datetime.now(UTC) - timedelta(days=1)
    ana = await make_client_profile(agency_id=email_world.agency_id, first_name="Ana")
    member = await make_email_member(
        campaign_id=uuid.UUID(cid),
        client_profile_id=ana.id,
        status="EMAIL_REPLIED",
        provider_lead_id="lea_ana",
        queued_at=sent_at - timedelta(hours=1),
        started_at=sent_at,
        current_step=1,
    )
    member_id = member.id
    render = await make_email_render(
        campaign_member_id=member_id,
        campaign_step_id=s1_id,
        step_position=1,
        subject="Hello Ana",
        body="Un mot.",
        provider_synced_at=sent_at - timedelta(minutes=30),
        sent_at=sent_at,
    )
    await make_email_message(
        campaign_member_id=member_id,
        campaign_id=uuid.UUID(cid),
        agency_id=email_world.agency_id,
        render_id=render.id,
        campaign_step_id=s1_id,
        step_position=1,
        status="OPENED",
        sent_at=sent_at,
        open_count=2,
        provider_message_id="act_sent",
    )
    await make_email_message(
        campaign_member_id=member_id,
        campaign_id=uuid.UUID(cid),
        agency_id=email_world.agency_id,
        direction="INBOUND",
        status="REPLIED",
        step_position=None,
        content_state="AVAILABLE",
        subject="Re: Hello Ana",
        body_text=None,
        body_html=(
            "<div>Oui, avec plaisir.</div><div class='gmail_quote'>Le lundi, X a écrit :"
            "<blockquote>Un mot.</blockquote></div>"
        ),
        received_at=sent_at + timedelta(hours=2),
        provider_message_id="act_reply",
    )
    await make_email_event(
        campaign_member_id=member_id, type="SENT", step_position=1, occurred_at=sent_at
    )
    await make_email_event(
        campaign_member_id=member_id,
        type="REPLIED",
        provider_type="emailsReplied",
        step_position=1,
        occurred_at=sent_at + timedelta(hours=2),
    )

    detail = (await client.get(f"/email/members/{member_id}", headers=headers)).json()
    assert detail["status"] == "EMAIL_REPLIED" and detail["full_name"] == "Ana Martin"
    assert [s["position"] for s in detail["steps"]] == [1, 2]
    first, second = detail["steps"]
    assert (first["status"], first["subject"], first["open_count"]) == ("OPENED", "Hello Ana", 2)
    assert first["step_id"] == str(s1_id) and first["planned_on"] is None
    assert [e["type"] for e in first["events"]] == ["REPLIED", "SENT"]
    # A reply tied to no step goes under the last email sent; HTML read as text.
    [reply] = first["replies"]
    assert reply["body_text"] == "Oui, avec plaisir."
    assert second["status"] is None and second["step_id"] == str(s2_id)
    # A reply ends the sequence: no later step is dated, and the hold says why.
    assert second["planned_on"] is None
    assert detail["schedule_hold"] == {"code": "sequence_ended", "params": {"reason": "replied"}}
    assert [c["direction"] for c in detail["conversation"]] == ["OUTBOUND", "INBOUND"]
    assert detail["conversation"][1]["body"] == "Oui, avec plaisir."
    assert len(detail["events"]) == 2 and detail["issues"] == []

    # Still to launch: what stands between it and a launch, template included.
    bare = await make_client_profile(agency_id=email_world.agency_id, first_name=None, email=None)
    waiting = await make_email_member(campaign_id=uuid.UUID(cid), client_profile_id=bare.id)
    shown = (await client.get(f"/email/members/{waiting.id}", headers=headers)).json()
    codes = [i["code"] for i in shown["issues"]]
    assert codes[0] == "NO_EMAIL" and "TEMPLATE" in codes
    template = next(i for i in shown["issues"] if i["code"] == "TEMPLATE")
    assert template["params"]["position"] == 1
    assert shown["schedule_hold"] == {"code": "not_launched", "params": {}}

    stranger = await client.get(f"/email/members/{member_id}", headers=email_world.other_headers)
    assert stranger.status_code == 404 and stranger.json()["code"] == "email.member_not_found"


# ── who sends ────────────────────────────────────────────────────────────────


async def test_a_campaign_is_sent_from_the_sender_chosen_in_nidria(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    make_email_step: MakeEmailStep,
) -> None:
    headers = email_world.headers
    listed = await client.get("/email/senders", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == {
        "external": True,
        "senders": [{"user_id": "usr_1", "email": "sender@nidria.test", "campaigns": 2}],
        "note": None,
        "error_code": None,
    }
    # Cached per team: Lemlist is asked once.
    await client.get("/email/senders", headers=headers)
    assert fake_email_provider.names().count("list_senders") == 1
    # Choosing a sender needs the right to manage campaigns.
    reader = await role_headers(make_role, make_agent, agent_headers, email_world.agency_id, *READ)
    refused = await client.get("/email/senders", headers=reader)
    assert refused.status_code == 403

    cid = (await campaign_via_api(client, headers, "Sent from"))["id"]
    settings = {
        "timezone": "Europe/Lisbon",
        "sending_days": [1, 2, 3, 4, 5],
        "sending_start_time": "09:00",
        "sending_end_time": "18:00",
        "daily_limit": 10,
        "default_delay_days": 2,
        "sender": {"user_id": "usr_1", "email": "sender@nidria.test"},
    }
    saved = await client.patch(
        f"/email/campaigns/{cid}", json={"settings": settings}, headers=headers
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["sender"] == {"user_id": "usr_1", "email": "sender@nidria.test"}
    # Live at Lemlist: starting names the chosen mailbox (the provider waits for it).
    await make_live(db_session, cid)
    await make_email_step(campaign_id=uuid.UUID(cid), provider_step_id="stp_1")
    await set_status(client, headers, cid, "ACTIVE")
    started = [kw for name, kw in fake_email_provider.ops if name == "start_campaign"]
    assert started == [{"campaign": "cam_live", "mailbox": "sender@nidria.test"}]
    await set_status(client, headers, cid, "PAUSED")
    await set_status(client, headers, cid, "ARCHIVED")
    assert fake_email_provider.names()[-2:] == ["pause_campaign", "archive_campaign"]

    # Lemlist could not be read: a code, never a sentence.
    email_manager.clear_caches()
    fake_email_provider.failures[("list_senders", "team")] = ProviderError(
        "Lemlist did not answer in time.", code="TIMEOUT"
    )
    failed = (await client.get("/email/senders", headers=headers)).json()
    assert (failed["note"], failed["error_code"]) == (
        "provider_error",
        "email.provider_unreachable",
    )


async def test_in_a_dry_run_nobody_is_asked_who_sends(
    client: AsyncClient, email_world: EmailWorld
) -> None:
    listed = await client.get("/email/senders", headers=email_world.headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == {
        "external": False,
        "senders": [],
        "note": "dry_run",
        "error_code": None,
    }
    meta = (await client.get("/email/meta", headers=email_world.headers)).json()
    assert (meta["provider_mode"], meta["team"], meta["team_mismatch"]) == ("DRY_RUN", None, False)


# ── changing and deleting a campaign live at Lemlist ─────────────────────────


async def test_a_live_campaign_changes_state_at_lemlist_only_with_email_sync(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    make_email_step: MakeEmailStep,
) -> None:
    editor = await role_headers(
        make_role, make_agent, agent_headers, email_world.agency_id, *READ, Permission.EMAIL_MANAGE
    )
    live = (await campaign_via_api(client, editor, "Live"))["id"]
    await make_email_step(campaign_id=uuid.UUID(live), provider_step_id="stp_1")
    await make_live(db_session, live, status="ACTIVE")
    refused = await client.patch(
        f"/email/campaigns/{live}", json={"status": "PAUSED"}, headers=editor
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "permission.denied"
    assert refused.json()["params"] == {"permission": "email.sync"}
    assert "pause_campaign" not in fake_email_provider.names()
    renamed = await client.patch(
        f"/email/campaigns/{live}", json={"name": "Renamed"}, headers=editor
    )
    assert renamed.status_code == 200
    # A refused change leaves the rest of the request undone.
    mixed = await client.patch(
        f"/email/campaigns/{live}", json={"name": "Not kept", "status": "PAUSED"}, headers=editor
    )
    assert mixed.status_code == 403
    assert (await client.get(f"/email/campaigns/{live}", headers=editor)).json()[
        "name"
    ] == "Renamed"

    # A dry-run campaign's state is the editor's alone: nothing leaves Nidria.
    dry = (await campaign_via_api(client, editor, "Dry"))["id"]
    await make_email_step(campaign_id=uuid.UUID(dry), provider_step_id="dry_stp_1")
    await make_live(db_session, dry, provider_id="dry_cam_1", team=None)
    assert (await set_status(client, editor, dry, "ACTIVE"))["provider_mode"] == "DRY_RUN"
    assert (await set_status(client, editor, dry, "PAUSED"))["status"] == "PAUSED"
    assert "start_campaign" not in fake_email_provider.names()

    # Lemlist refuses: a code, the state unchanged.
    fake_email_provider.failures[("pause_campaign", "cam_live")] = ProviderError(
        "Lemlist: campaign-reroute-in-progress", code="RETRY_LATER"
    )
    held = await client.patch(
        f"/email/campaigns/{live}", json={"status": "PAUSED"}, headers=email_world.headers
    )
    assert held.status_code == 409 and held.json()["code"] == "email.provider_retry_later"
    assert (await client.get(f"/email/campaigns/{live}", headers=editor)).json()[
        "status"
    ] == "ACTIVE"


async def test_a_real_campaign_never_changes_while_the_agency_runs_dry(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
    make_email_step: MakeEmailStep,
) -> None:
    """The agency runs in dry run (no `fake_email_provider`): a campaign that exists
    at Lemlist is never changed behind its back."""
    headers = email_world.headers
    cid = (await campaign_via_api(client, headers, "Real"))["id"]
    await make_email_step(campaign_id=uuid.UUID(cid), provider_step_id="stp_1")
    # Created in the team the agency was bound to; the agency runs dry now.
    await make_live(db_session, cid, status="ACTIVE", team="tea_bound")
    refused = await client.patch(
        f"/email/campaigns/{cid}", json={"status": "PAUSED"}, headers=headers
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "email.provider_unavailable"
    assert refused.json()["params"] == {"reason": "external_effects_off"}
    deleted = await client.delete(f"/email/campaigns/{cid}", headers=headers)
    assert deleted.status_code == 409 and deleted.json()["code"] == "email.provider_unavailable"
    person = await make_client_profile(agency_id=email_world.agency_id)
    member = await make_email_member(
        campaign_id=uuid.UUID(cid),
        client_profile_id=person.id,
        status="EMAIL_ACTIVE",
        provider_lead_id="lea_real",
        queued_at=datetime.now(UTC),
    )
    paused = await act(client, headers, "pause", [member.id])
    assert paused["rows"][0]["code"] == "email.provider_unavailable"
    assert paused["rows"][0]["params"] == {"reason": "external_effects_off"}
    assert (await members_of(db_session, cid))[0]["status"] == "EMAIL_ACTIVE"


async def test_deleting_a_live_campaign_pauses_it_in_lemlist_then_removes_it(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
    make_email_event: MakeEmailEvent,
) -> None:
    headers = email_world.headers
    cid = (await campaign_via_api(client, headers, "To delete"))["id"]
    await make_live(db_session, cid, status="ACTIVE")
    ana = await make_client_profile(agency_id=email_world.agency_id)
    member = await make_email_member(
        campaign_id=uuid.UUID(cid),
        client_profile_id=ana.id,
        status="EMAIL_ACTIVE",
        provider_lead_id="lea_ana",
        queued_at=datetime.now(UTC),
    )
    await make_email_event(campaign_member_id=member.id, provider_event_id="act_gone")
    fake_email_provider.ops.clear()
    url = f"/email/campaigns/{cid}"

    # It takes the right to manage campaigns, and email.sync while live.
    reader = await role_headers(make_role, make_agent, agent_headers, email_world.agency_id, *READ)
    assert (await client.delete(url, headers=reader)).status_code == 403
    editor = await role_headers(
        make_role, make_agent, agent_headers, email_world.agency_id, *READ, Permission.EMAIL_MANAGE
    )
    no_sync = await client.delete(url, headers=editor)
    assert no_sync.status_code == 403 and no_sync.json()["params"] == {"permission": "email.sync"}
    # Another agency's id is unknown.
    other = await client.delete(url, headers=email_world.other_headers)
    assert other.status_code == 404 and other.json()["code"] == "email.campaign_not_found"
    # Without external effects Lemlist cannot be told to stop: nothing is deleted.
    fake_email_provider.external = False
    assert (await client.delete(url, headers=headers)).status_code == 409
    fake_email_provider.external = True
    assert fake_email_provider.ops == []

    deleted = await client.delete(url, headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "id": cid,
        "name": "To delete",
        "provider_paused": True,
        "provider_kept": True,
    }
    assert fake_email_provider.names() == ["pause_campaign"]
    assert (
        await db_session.scalar(select(EmailCampaign.id).where(EmailCampaign.id == uuid.UUID(cid)))
        is None
    )
    assert await members_of(db_session, cid) == []
    events = select(EmailProviderEvent.id).where(EmailProviderEvent.provider_event_id == "act_gone")
    assert (await db_session.execute(events)).all() == []
    assert (await client.get(url, headers=headers)).status_code == 404


async def test_deleting_a_draft_or_another_teams_campaign_never_reaches_lemlist(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
) -> None:
    headers = email_world.headers
    draft = (await campaign_via_api(client, headers, "Never synced"))["id"]
    old = (await campaign_via_api(client, headers, "Old team"))["id"]
    await make_live(db_session, old, provider_id="cam_old", team="tea_previous", status="ACTIVE")
    detail = (await client.get(f"/email/campaigns/{old}", headers=headers)).json()
    assert detail["team_mismatch"] is True and detail["provider_mode"] == "LIVE"
    fake_email_provider.ops.clear()
    for cid in (draft, old):
        deleted = await client.delete(f"/email/campaigns/{cid}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert (deleted.json()["provider_paused"], deleted.json()["provider_kept"]) == (
            False,
            False,
        )
    assert fake_email_provider.ops == []
    listed = (await client.get("/email/campaigns", headers=headers)).json()
    assert {c["id"] for c in listed}.isdisjoint({draft, old})


async def test_another_teams_campaign_is_read_only(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
    make_email_step: MakeEmailStep,
) -> None:
    """Rule N4: a campaign Lemlist holds in another team than the key's is
    history; an unstamped real campaign is one too (decision A4)."""
    headers = email_world.headers
    for team in ("tea_previous", None):
        cid = (await campaign_via_api(client, headers, f"Team {team}"))["id"]
        step = await make_email_step(campaign_id=uuid.UUID(cid), provider_step_id="stp_x")
        step_id = step.id
        await make_live(db_session, cid, provider_id=f"cam_{team}", team=team, status="ACTIVE")
        person = await make_client_profile(agency_id=email_world.agency_id)
        member = await make_email_member(
            campaign_id=uuid.UUID(cid),
            client_profile_id=person.id,
            status="EMAIL_ACTIVE",
            provider_lead_id="lea_x",
            queued_at=datetime.now(UTC),
        )
        detail = (await client.get(f"/email/campaigns/{cid}", headers=headers)).json()
        assert detail["team_mismatch"] is True
        assert detail["sequence"]["reasons"]["can_add_step"] == "team_mismatch"
        assert detail["steps"][0]["capabilities"]["delete_reason"] == "team_mismatch"
        for method, path, body in (
            ("PATCH", f"/email/campaigns/{cid}", {"name": "x"}),
            ("POST", f"/email/campaigns/{cid}/steps", {"name": "x", "subject_template": "s"}),
            ("PATCH", f"/email/steps/{step_id}", {"body_template": "x"}),
            ("DELETE", f"/email/steps/{step_id}", None),
        ):
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status_code == 409, (method, path, response.text)
            assert response.json()["code"] == "email.team_mismatch"
        for action in ("pause", "resume"):
            result = await act(client, headers, action, [member.id])
            assert result["rows"][0]["code"] == "email.team_mismatch", action
        # Removed from Nidria only: another team's lead is left alone.
        assert (await act(client, headers, "remove", [member.id]))["rows"][0]["ok"] is True
    assert fake_email_provider.ops == []


async def test_meta_says_what_the_agent_may_do_and_what_a_template_reads(
    client: AsyncClient,
    email_world: EmailWorld,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    meta = (await client.get("/email/meta", headers=email_world.headers)).json()
    assert meta["enabled"] is True and meta["provider"] == "LEMLIST"
    flags = ("can_manage", "can_enroll", "can_pause", "can_remove", "can_sync")
    assert [meta[f] for f in flags] == [True] * 5
    assert meta["campaign_statuses"] == ["DRAFT", "ACTIVE", "PAUSED", "ARCHIVED"]
    assert "EMAIL_UNSUBSCRIBED" in meta["member_statuses"]
    assert meta["client_statuses"] == ["prospect", "client"]
    tokens = {v["token"]: v for v in meta["variables"]}
    assert {"client.first_name", "company.name", "agency.name"} <= set(tokens)
    # Decision A12: a token, its source, type and example; no English label.
    assert set(tokens["client.nationality"]) == {"token", "source", "type", "example"}
    assert (tokens["client.nationality"]["source"], tokens["client.nationality"]["type"]) == (
        "client",
        "country",
    )
    pauser = await role_headers(
        make_role, make_agent, agent_headers, email_world.agency_id, *READ, Permission.EMAIL_PAUSE
    )
    limited = (await client.get("/email/meta", headers=pauser)).json()
    assert [limited[f] for f in flags] == [False, False, True, False, False]
