"""A campaign Lemlist already holds leads of: its sequence only grows at the
end (Lemlist refuses inserts and removals then, measured by Prism on
2026-10-05), and a recipient never reaches a step without its own email.

Ported from Prism's `tests/test_email_active_sequence.py` (reference
629a2b0), the part phase 3A serves: what the sequence allows, the impact of
a new step, parking the last step nobody got, the frozen renders and the
resume rule. The Sync that pushes a new step's emails and puts a parked step
back to work is phase 3B; here the campaign is stood up as a launch and a
sync left it (the email plugin's factories).

Four recipients in a live campaign of two steps:
- Ana got step 1 and waits for step 2;
- Ben got step 1 and replied: stopped;
- Cleo got both steps: completed;
- Dan was handed over and has been sent nothing yet.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import EmailCampaignMember, EmailCampaignStep, EmailMemberRender
from src.core.rbac.permissions import Permission
from src.email_campaigns.providers.base import CampaignRef, ProviderError
from src.email_campaigns.renderer import content_hash
from tests.email_fakes import FakeProvider
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    EmailWorld,
    MakeEmailMember,
    MakeEmailRender,
    MakeEmailStep,
)
from tests.plugins.rbac_plugin import MakeRole
from tests.test_email_campaigns import (
    READ,
    act,
    campaign_via_api,
    make_live,
    members_of,
    role_headers,
    set_status,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

LONG_AGO = datetime.now(UTC) - timedelta(days=10)
LAUNCHED = datetime.now(UTC) - timedelta(days=9)


@dataclass
class Live:
    campaign_id: str
    #: position -> step id
    steps: dict[int, uuid.UUID]
    #: first name (lowercase) -> membership id, lead id
    members: dict[str, uuid.UUID]
    leads: dict[str, str]


async def _step(
    make_email_step: MakeEmailStep, campaign_id: str, position: int, **fields: Any
) -> uuid.UUID:
    data: dict[str, Any] = {
        "position": position,
        "name": f"Step {position}",
        "subject_template": "Hello {{client.first_name}}" if position == 1 else "",
        "body_template": f"Step {position}.",
        "provider_step_id": f"stp_{position}",
        "delay_value": 0 if position == 1 else 3,
        "created_at": LONG_AGO,
        **fields,
    }
    data.setdefault(
        "content_hash", content_hash(data["subject_template"], data["body_template"], {})
    )
    data.setdefault("provider_synced_hash", data["content_hash"])
    step = await make_email_step(campaign_id=uuid.UUID(campaign_id), **data)
    return step.id


async def _live(
    client: AsyncClient,
    db: AsyncSession,
    world: EmailWorld,
    provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
    make_email_step: MakeEmailStep,
    make_email_render: MakeEmailRender,
) -> Live:
    cid = (await campaign_via_api(client, world.headers, "Active"))["id"]
    steps = {
        1: await _step(make_email_step, cid, 1),
        2: await _step(make_email_step, cid, 2),
    }
    await make_live(db, cid, status="ACTIVE")
    provider.sequences["cam_live"] = ["stp_1", "stp_2"]
    progress = {
        "ana": ("EMAIL_ACTIVE", 1, (1,), (2,)),
        "ben": ("EMAIL_REPLIED", 1, (1,), ()),
        "cleo": ("EMAIL_COMPLETED", 2, (1, 2), ()),
        "dan": ("EMAIL_QUEUED", None, (), (1, 2)),
    }
    members: dict[str, uuid.UUID] = {}
    leads: dict[str, str] = {}
    for name, (status, current, sent, synced) in progress.items():
        person = await make_client_profile(
            agency_id=world.agency_id, first_name=name.title(), email=f"{name}@x.io"
        )
        member = await make_email_member(
            campaign_id=uuid.UUID(cid),
            client_profile_id=person.id,
            status=status,
            current_step=current,
            provider_lead_id=f"lea_{name}",
            provider_contact_id=f"ctc_{name}",
            queued_at=LAUNCHED,
            started_at=LAUNCHED if sent else None,
        )
        members[name], leads[name] = member.id, f"lea_{name}"
        for position in (*sent, *synced):
            await _render(make_email_render, db, member.id, steps, position, sent=position in sent)
    provider.ops.clear()
    return Live(cid, steps, members, leads)


async def _render(
    make_email_render: MakeEmailRender,
    db: AsyncSession,
    member_id: uuid.UUID,
    steps: dict[int, uuid.UUID],
    position: int,
    *,
    sent: bool,
) -> None:
    step = await db.get(EmailCampaignStep, steps[position])
    assert step is not None
    await make_email_render(
        campaign_member_id=member_id,
        campaign_step_id=steps[position],
        step_position=position,
        subject=f"Rendered {position}",
        body=f"Rendered body {position}",
        template_version=step.version,
        template_hash=step.content_hash,
        provider_synced_at=LAUNCHED,
        sent_at=LAUNCHED + timedelta(days=position) if sent else None,
    )


@pytest.fixture
async def live(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    fake_email_provider: FakeProvider,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
    make_email_step: MakeEmailStep,
    make_email_render: MakeEmailRender,
) -> Live:
    return await _live(
        client,
        db_session,
        email_world,
        fake_email_provider,
        make_client_profile,
        make_email_member,
        make_email_step,
        make_email_render,
    )


async def _detail(client: AsyncClient, world: EmailWorld, live: Live) -> dict[str, Any]:
    response = await client.get(f"/email/campaigns/{live.campaign_id}", headers=world.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _follow_up(
    client: AsyncClient, world: EmailWorld, live: Live, **fields: Any
) -> dict[str, Any]:
    body = {"name": "Follow-up 2", "subject_template": "", "body_template": "Step three.", **fields}
    response = await client.post(
        f"/email/campaigns/{live.campaign_id}/steps", json=body, headers=world.headers
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# ── appending ────────────────────────────────────────────────────────────────


async def test_an_active_campaign_appends_a_follow_up_for_whoever_can_still_reach_it(
    client: AsyncClient, email_world: EmailWorld, live: Live, fake_email_provider: FakeProvider
) -> None:
    impact = (
        await client.get(
            f"/email/campaigns/{live.campaign_id}/steps/impact", headers=email_world.headers
        )
    ).json()
    assert impact["mode"] == "APPEND" and impact["allowed"] is True and impact["position"] == 3
    # Ana and Dan can still reach it; nobody gets it retroactively.
    assert (impact["total"], impact["will_receive"], impact["already_past"]) == (4, 2, 0)
    assert (impact["completed"], impact["replied"], impact["paused"]) == (1, 1, 0)

    detail = await _follow_up(client, email_world, live)
    new = detail["steps"][2]
    assert new["position"] == 3 and new["provider_step_id"] is None and new["unsynced"] is True
    assert new["capabilities"]["pending"] is True
    assert new["capabilities"]["added_after_launch"] is True
    assert detail["steps"][0]["capabilities"]["added_after_launch"] is False
    assert detail["sequence"]["active_sequence"] is True
    assert detail["sequence"]["can_add_step"] is True
    assert detail["sequence"]["reasons"] == {
        "can_insert_step": "insert_in_use",
        "can_reorder_steps": "reorder_in_use",
    }
    assert detail["unsynced_changes"] is True
    # Nothing reaches Lemlist until the next Sync (phase 3B) adds it there.
    assert fake_email_provider.ops == []


async def test_an_active_campaign_refuses_an_insert_a_reorder_and_removing_a_used_step(
    client: AsyncClient, email_world: EmailWorld, live: Live, fake_email_provider: FakeProvider
) -> None:
    cid, headers = live.campaign_id, email_world.headers
    impact = (
        await client.get(f"/email/campaigns/{cid}/steps/impact?position=2", headers=headers)
    ).json()
    assert impact["mode"] == "INSERT" and impact["allowed"] is False
    assert impact["reason"] == "insert_in_use"
    # Ana waits for step 2: an insert there would have reached her.
    assert impact["will_receive"] == 2 and impact["already_past"] == 0

    insert = await client.post(
        f"/email/campaigns/{cid}/steps",
        json={"name": "Between", "position": 2, "subject_template": ""},
        headers=headers,
    )
    assert insert.status_code == 409 and insert.json()["code"] == "email.step_insert_in_use"
    reorder = await client.post(
        f"/email/campaigns/{cid}/steps/reorder",
        json={"step_ids": [str(live.steps[2]), str(live.steps[1])]},
        headers=headers,
    )
    assert reorder.status_code == 409 and reorder.json()["code"] == "email.sequence_in_use"
    # The same order is no change: accepted.
    same = await client.post(
        f"/email/campaigns/{cid}/steps/reorder",
        json={"step_ids": [str(live.steps[1]), str(live.steps[2])]},
        headers=headers,
    )
    assert same.status_code == 200

    detail = await _detail(client, email_world, live)
    first, second = (s["capabilities"] for s in detail["steps"])
    assert (first["can_delete"], first["delete_reason"]) == (False, "first_step_in_use")
    # Cleo got step 2: it stays, with its history; skipping is Lemlist's app's.
    assert (second["can_delete"], second["reached"]) == (False, 1)
    assert second["delete_reason"] == "already_sent"
    assert second["can_skip"] is False and second["skip_reason"] == "provider_app_only"
    for position, reason in ((2, "already_sent"), (1, "first_step_in_use")):
        removal = await client.delete(f"/email/steps/{live.steps[position]}", headers=headers)
        assert removal.status_code == 409
        assert removal.json()["code"] == "email.step_delete_in_use"
        assert removal.json()["params"] == {"reason": reason}
    assert {"delete_step", "park_step", "create_step", "reorder_steps"}.isdisjoint(
        fake_email_provider.names()
    )
    assert len((await _detail(client, email_world, live))["steps"]) == 2


async def _with_a_third_step_at_lemlist(
    db: AsyncSession, live: Live, provider: FakeProvider, make_email_step: MakeEmailStep
) -> uuid.UUID:
    """Step 3 is at Lemlist and nobody got it; Ana got step 2 and waits for it."""
    live.steps[3] = await _step(
        make_email_step, live.campaign_id, 3, created_at=datetime.now(UTC) - timedelta(days=1)
    )
    provider.sequences["cam_live"].append("stp_3")
    member = await db.get(EmailCampaignMember, live.members["ana"])
    assert member is not None
    member.current_step = 2
    await db.commit()
    return live.steps[3]


async def test_the_last_step_nobody_got_is_parked_and_left_for_reuse(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    live: Live,
    fake_email_provider: FakeProvider,
    make_email_step: MakeEmailStep,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    third = await _with_a_third_step_at_lemlist(
        db_session, live, fake_email_provider, make_email_step
    )
    detail = await _detail(client, email_world, live)
    assert detail["steps"][2]["capabilities"]["can_delete"] is True
    # Step 2 is no longer the last one at the provider.
    assert detail["steps"][1]["capabilities"]["delete_reason"] == "not_last_at_provider"

    # Parking a step Lemlist holds acts there: it takes email.sync.
    editor = await role_headers(
        make_role, make_agent, agent_headers, email_world.agency_id, *READ, Permission.EMAIL_MANAGE
    )
    refused = await client.delete(f"/email/steps/{third}", headers=editor)
    assert refused.status_code == 403
    assert refused.json()["params"] == {"permission": "email.sync"}
    assert fake_email_provider.ops == []

    removed = await client.delete(f"/email/steps/{third}", headers=email_world.headers)
    assert removed.status_code == 200, removed.text
    assert ("park_step", {"step": "stp_3"}) in fake_email_provider.ops
    assert "delete_step" not in fake_email_provider.names()
    assert [s["position"] for s in removed.json()["steps"]] == [1, 2]
    # Ana waited for it: nothing comes after, her sequence is over. Dan, who
    # has not reached step 2, goes on.
    rows = {m["id"]: m for m in await members_of(db_session, live.campaign_id)}
    assert rows[live.members["ana"]]["status"] == "EMAIL_COMPLETED"
    assert rows[live.members["dan"]]["status"] == "EMAIL_QUEUED"

    # Lemlist keeps the step, out of reach (a 1500-day delay): the next
    # follow-up is added in Nidria only, and the Sync puts the parked step
    # back to work for it instead of adding one behind it (phase 3B).
    ref = CampaignRef("cam_live", "seq_live", "skd_live")
    assert [(s.step_id, s.delay_days) for s in fake_email_provider.read_steps(ref)][-1] == (
        "stp_3",
        1500,
    )
    again = await _follow_up(client, email_world, live, body_template="Again.")
    assert again["steps"][2]["provider_step_id"] is None
    assert again["steps"][2]["capabilities"]["pending"] is True
    assert "create_step" not in fake_email_provider.names()


async def test_a_park_lemlist_refuses_changes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    live: Live,
    fake_email_provider: FakeProvider,
    make_email_step: MakeEmailStep,
) -> None:
    third = await _with_a_third_step_at_lemlist(
        db_session, live, fake_email_provider, make_email_step
    )
    fake_email_provider.failures[("park_step", "stp_3")] = ProviderError(
        "Lemlist: remove-step-on-sequence-in-use", code="STEP_IN_USE"
    )
    refused = await client.delete(f"/email/steps/{third}", headers=email_world.headers)
    assert refused.status_code == 409
    assert refused.json()["code"] == "email.provider_step_in_use"
    assert len((await _detail(client, email_world, live))["steps"]) == 3
    rows = {m["id"]: m for m in await members_of(db_session, live.campaign_id)}
    assert rows[live.members["ana"]]["status"] == "EMAIL_ACTIVE"


async def test_a_pending_step_is_dropped_without_reaching_the_provider(
    client: AsyncClient, email_world: EmailWorld, live: Live, fake_email_provider: FakeProvider
) -> None:
    detail = await _follow_up(client, email_world, live)
    removed = await client.delete(
        f"/email/steps/{detail['steps'][2]['id']}", headers=email_world.headers
    )
    assert removed.status_code == 200 and len(removed.json()["steps"]) == 2
    assert fake_email_provider.ops == []


async def test_a_used_steps_copy_goes_at_the_end(
    client: AsyncClient, email_world: EmailWorld, live: Live, fake_email_provider: FakeProvider
) -> None:
    copied = await client.post(
        f"/email/steps/{live.steps[1]}/duplicate",
        json={"name": "First again"},
        headers=email_world.headers,
    )
    assert copied.status_code == 201
    steps = copied.json()["steps"]
    assert [s["name"] for s in steps] == ["Step 1", "Step 2", "First again"]
    assert steps[2]["provider_step_id"] is None and steps[2]["capabilities"]["pending"] is True
    # The original steps keep their provider steps: nothing is remapped.
    assert [s["provider_step_id"] for s in steps[:2]] == ["stp_1", "stp_2"]
    assert fake_email_provider.ops == []


# ── content ──────────────────────────────────────────────────────────────────


async def test_a_sent_email_never_changes_and_unsent_ones_are_outdated(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    live: Live,
) -> None:
    headers = email_world.headers
    edited = await client.patch(
        f"/email/steps/{live.steps[1]}",
        json={"body_template": "Step one, rewritten."},
        headers=headers,
    )
    assert edited.status_code == 200
    assert edited.json()["version"] == 2 and edited.json()["unsynced"] is True

    ana = (await client.get(f"/email/members/{live.members['ana']}", headers=headers)).json()
    dan = (await client.get(f"/email/members/{live.members['dan']}", headers=headers)).json()
    # Ana's step 1 went out: it stays what was sent. Dan's is not sent yet:
    # the next sync renders the new version for him (phase 3B).
    assert (ana["steps"][0]["outdated"], ana["steps"][0]["template_version"]) == (False, 1)
    assert ana["steps"][0]["body"] == "Rendered body 1"
    assert (dan["steps"][0]["outdated"], dan["steps"][0]["status"]) == (True, "QUEUED")
    sent = await db_session.scalar(
        select(EmailMemberRender.id).where(
            EmailMemberRender.campaign_member_id == live.members["ana"],
            EmailMemberRender.step_position == 1,
        )
    )
    # The database refuses it too, whatever writes.
    with pytest.raises(DBAPIError):
        await db_session.execute(
            text("UPDATE email_member_render SET body = 'x' WHERE id = :id"), {"id": sent}
        )
    await db_session.rollback()


# ── pause and resume ─────────────────────────────────────────────────────────


async def test_a_recipient_is_resumed_only_once_its_own_email_is_at_lemlist(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    live: Live,
    fake_email_provider: FakeProvider,
    make_email_step: MakeEmailStep,
    make_email_render: MakeEmailRender,
) -> None:
    """Lemlist sends a step's placeholder to a lead with no content for it:
    a recipient who could reach a step without its own email is never
    resumed (the Sync renders it, then resumes, phase 3B)."""
    headers = email_world.headers
    paused = await act(client, headers, "pause", [live.members["dan"]])
    assert paused["rows"][0]["status"] == "EMAIL_PAUSED"
    await _with_a_third_step_at_lemlist(db_session, live, fake_email_provider, make_email_step)

    refused = await act(client, headers, "resume", [live.members["dan"]])
    assert refused["rows"][0]["ok"] is False
    assert refused["rows"][0]["code"] == "email.member_needs_render"
    assert refused["rows"][0]["params"] == {"position": 3}
    assert "resume_contact" not in fake_email_provider.names()

    await _render(make_email_render, db_session, live.members["dan"], live.steps, 3, sent=False)
    resumed = await act(client, headers, "resume", [live.members["dan"]])
    assert resumed["rows"][0]["ok"] is True and resumed["rows"][0]["status"] == "EMAIL_QUEUED"
    assert ("resume_contact", {"lead": "lea_dan"}) in fake_email_provider.ops


async def test_a_pause_names_the_agent_until_the_recipient_is_resumed(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    live: Live,
    fake_email_provider: FakeProvider,
) -> None:
    headers = email_world.headers
    ana = live.members["ana"]
    await act(client, headers, "pause", [ana])
    shown = (await client.get(f"/email/members/{ana}", headers=headers)).json()
    assert shown["status"] == "EMAIL_PAUSED"
    assert shown["paused_by_agent_id"] == str(email_world.admin_id)
    assert shown["schedule_hold"] == {"code": "member_paused", "params": {}}
    # An ended recipient is neither paused nor resumed.
    ben = await act(client, headers, "pause", [live.members["ben"]])
    assert ben["rows"][0]["code"] == "email.member_not_pausable"
    assert ben["rows"][0]["params"] == {"status": "EMAIL_REPLIED"}
    cleo = await act(client, headers, "resume", [live.members["cleo"]])
    assert cleo["rows"][0]["code"] == "email.member_not_resumable"

    resumed = await act(client, headers, "resume", [ana])
    assert resumed["rows"][0]["status"] == "EMAIL_ACTIVE"  # she was sent an email already
    rows = {m["id"]: m for m in await members_of(db_session, live.campaign_id)}
    assert rows[ana]["paused_by_agent_id"] is None
    assert fake_email_provider.names() == ["pause_contact", "resume_contact"]
    # Lemlist refuses: the row says so with a code, the recipient unchanged.
    fake_email_provider.failures[("pause_contact", "lea_dan")] = ProviderError(
        "Lemlist: upgrade your plan", code="PLAN_REQUIRED", status=402
    )
    refused = await act(client, headers, "pause", [live.members["dan"]])
    assert refused["rows"][0]["code"] == "email.provider_plan_required"
    rows = {m["id"]: m for m in await members_of(db_session, live.campaign_id)}
    assert rows[live.members["dan"]]["status"] == "EMAIL_QUEUED"


# ── what the sequence allows, and who a step reaches ─────────────────────────


async def test_an_archived_sequence_allows_nothing(
    client: AsyncClient, email_world: EmailWorld, live: Live, fake_email_provider: FakeProvider
) -> None:
    archived = await set_status(client, email_world.headers, live.campaign_id, "ARCHIVED")
    # Archiving pauses the campaign at Lemlist (it has no archive).
    assert fake_email_provider.names() == ["archive_campaign"]
    sequence = archived["sequence"]
    assert sequence["active_sequence"] is True
    assert not any(
        sequence[k]
        for k in (
            "can_edit_step_content",
            "can_edit_step_delay",
            "can_add_step",
            "can_insert_step",
            "can_reorder_steps",
        )
    )
    assert set(sequence["reasons"].values()) == {"archived"}


async def test_who_a_new_step_reaches_counts_each_recipient_once(
    client: AsyncClient,
    email_world: EmailWorld,
    live: Live,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
) -> None:
    for name, status in (
        ("eve", "EMAIL_REMOVED"),
        ("fred", "EMAIL_PAUSED"),
        ("gus", "EMAIL_BOUNCED"),
        ("hal", "EMAIL_UNSUBSCRIBED"),
        ("ivy", "EMAIL_READY"),
    ):
        person = await make_client_profile(agency_id=email_world.agency_id, first_name=name)
        await make_email_member(
            campaign_id=uuid.UUID(live.campaign_id), client_profile_id=person.id, status=status
        )
    impact = (
        await client.get(
            f"/email/campaigns/{live.campaign_id}/steps/impact?position=9",
            headers=email_world.headers,
        )
    ).json()
    assert impact["position"] == 3  # past the end: the end
    assert impact == {
        "position": 3,
        "mode": "APPEND",
        "allowed": True,
        "reason": None,
        "total": 9,
        "will_receive": 3,  # Ana, Dan, and Ivy once launched
        "already_past": 0,
        "completed": 1,
        "paused": 1,
        "replied": 1,
        "removed": 1,
        "ended": 2,
    }
