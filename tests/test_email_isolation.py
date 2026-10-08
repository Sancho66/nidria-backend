"""The Email module stays in its agency, and each permission gates its own
actions, on the backend.

Ported from Prism's `tests/test_email_isolation.py` (reference 629a2b0),
where two projects stood for two agencies: the routes of phases 3A and 3B
(the job and the webhook come in phase 4). Nidria's own (spec 4.2, 4.3,
8.10): every route is bound, the boot check fails without one, `case.view`
is required next to `email.*`, a module switched off answers 409
everywhere, an expat token is refused, a bulk action checks every row even
when a route's binding is loosened in data, and the Launch and the Sync are
refused under impersonation (D6).

Two agencies, each with a campaign, a step, an AI variable, a client and a
membership. Every cross-agency id is tried on the other agency's token;
every route is tried with each missing permission.
"""

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.email_campaign import (
    EmailCampaign,
    EmailCampaignAiVariable,
    EmailCampaignMember,
)
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import ProtectedResource
from src.core.enums import Audience
from src.core.rbac.baseline import collect_bindings
from src.core.rbac.integrity import StartupError, assert_all_routes_bound
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_router import BINDINGS
from tests.email_fakes import FakeProvider
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import (
    EmailWorld,
    MakeEmailMember,
    email_env,
)
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.rbac_plugin import MakeRole
from tests.test_email_campaigns import (
    READ,
    campaign_via_api,
    role_headers,
    set_status,
    step_via_api,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

VIEW = Permission.EMAIL_VIEW
MANAGE = Permission.EMAIL_MANAGE
SYNC = Permission.EMAIL_SYNC
ALL_EMAIL = (
    Permission.EMAIL_VIEW,
    Permission.EMAIL_MANAGE,
    Permission.EMAIL_ENROLL,
    Permission.EMAIL_PAUSE,
    Permission.EMAIL_REMOVE,
    Permission.EMAIL_SYNC,
)
#: (method, path) -> the permission its binding carries (spec 7.1).
EXPECTED_BINDINGS: dict[tuple[str, str], Permission] = {
    ("GET", "/email/meta"): VIEW,
    ("GET", "/email/variables/fill"): MANAGE,
    ("GET", "/email/senders"): MANAGE,
    ("GET", "/email/campaigns"): VIEW,
    ("POST", "/email/campaigns"): MANAGE,
    ("GET", "/email/campaigns/{campaign_id}"): VIEW,
    ("PATCH", "/email/campaigns/{campaign_id}"): MANAGE,
    ("POST", "/email/campaigns/{campaign_id}/duplicate"): MANAGE,
    ("DELETE", "/email/campaigns/{campaign_id}"): MANAGE,
    ("GET", "/email/campaigns/{campaign_id}/steps"): VIEW,
    ("GET", "/email/campaigns/{campaign_id}/steps/impact"): VIEW,
    ("POST", "/email/campaigns/{campaign_id}/steps"): MANAGE,
    ("POST", "/email/campaigns/{campaign_id}/steps/reorder"): MANAGE,
    ("PATCH", "/email/steps/{step_id}"): MANAGE,
    ("DELETE", "/email/steps/{step_id}"): MANAGE,
    ("POST", "/email/steps/{step_id}/duplicate"): MANAGE,
    ("POST", "/email/steps/{step_id}/ai-variables"): MANAGE,
    ("PATCH", "/email/ai-variables/{variable_id}"): MANAGE,
    ("DELETE", "/email/ai-variables/{variable_id}"): MANAGE,
    ("POST", "/email/ai-variables/test"): MANAGE,
    ("POST", "/email/templates/preview"): MANAGE,
    ("GET", "/email/members"): VIEW,
    ("GET", "/email/members/{member_id}"): VIEW,
    ("POST", "/email/members/pause"): Permission.EMAIL_PAUSE,
    ("POST", "/email/members/resume"): Permission.EMAIL_PAUSE,
    ("POST", "/email/members/remove"): Permission.EMAIL_REMOVE,
    ("POST", "/email/members/move"): Permission.EMAIL_ENROLL,
    # phase 3B
    ("POST", "/email/campaigns/{campaign_id}/sync"): SYNC,
    ("GET", "/email/outreach"): VIEW,
    ("GET", "/email/campaigns/{campaign_id}/performance"): VIEW,
    ("GET", "/email/campaigns/{campaign_id}/launch"): VIEW,
    ("POST", "/email/campaigns/{campaign_id}/launch"): SYNC,
    ("GET", "/email/client-profiles/{client_profile_id}"): VIEW,
    ("GET", "/email/candidates/client-profiles"): VIEW,
    ("GET", "/email/candidates/companies"): VIEW,
    ("POST", "/email/campaigns/{campaign_id}/members/preview-add"): Permission.EMAIL_ENROLL,
    ("POST", "/email/campaigns/{campaign_id}/members/add"): Permission.EMAIL_ENROLL,
    # lot C: the import of the Lemlist campaigns already running
    ("GET", "/email/import/lemlist"): SYNC,
    ("POST", "/email/import/lemlist"): SYNC,
}
#: Under impersonation, refused whatever the target may do (D6, lot C).
IMPERSONATION_DENIED = {
    ("POST", "/email/campaigns/{campaign_id}/launch"),
    ("POST", "/email/campaigns/{campaign_id}/sync"),
    ("GET", "/email/import/lemlist"),
    ("POST", "/email/import/lemlist"),
}


@dataclass
class Side:
    """One agency's Email objects, as plain ids."""

    headers: dict[str, str]
    agency_id: uuid.UUID
    campaign_id: str
    step_id: str
    ai_variable_id: str
    profile_id: str
    member_id: str


async def _side(
    client: AsyncClient,
    headers: dict[str, str],
    agency_id: uuid.UUID,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
    name: str,
) -> Side:
    campaign = await campaign_via_api(client, headers, f"{name} campaign")
    step = (await step_via_api(client, headers, campaign["id"]))["steps"][0]
    with_ai = await client.post(
        f"/email/steps/{step['id']}/ai-variables",
        json={"key": "hook", "prompt_template": "Une phrase sur {{client.first_name}}."},
        headers=headers,
    )
    assert with_ai.status_code == 201, with_ai.text
    person = await make_client_profile(
        agency_id=agency_id, first_name=name, email=f"{name.lower()}@x.io"
    )
    member = await make_email_member(
        campaign_id=uuid.UUID(campaign["id"]), client_profile_id=person.id, agency_id=agency_id
    )
    return Side(
        headers=headers,
        agency_id=agency_id,
        campaign_id=campaign["id"],
        step_id=step["id"],
        ai_variable_id=with_ai.json()["ai_variables"][0]["id"],
        profile_id=str(person.id),
        member_id=str(member.id),
    )


@dataclass
class Both:
    here: Side
    there: Side


@pytest.fixture
async def both(
    client: AsyncClient,
    email_world: EmailWorld,
    make_client_profile: MakeClientProfile,
    make_email_member: MakeEmailMember,
) -> Both:
    return Both(
        here=await _side(
            client,
            email_world.headers,
            email_world.agency_id,
            make_client_profile,
            make_email_member,
            "Lisa",
        ),
        there=await _side(
            client,
            email_world.other_headers,
            email_world.other_agency_id,
            make_client_profile,
            make_email_member,
            "Pedro",
        ),
    )


def _routes(s: Side) -> list[tuple[str, str, Any]]:
    """Every agent route of phases 3A and 3B and lot C, with valid bodies,
    on `s`'s objects."""
    campaign, step, variable, member = s.campaign_id, s.step_id, s.ai_variable_id, s.member_id
    return [
        ("GET", "/email/meta", None),
        ("GET", "/email/variables/fill", None),
        ("GET", "/email/senders", None),
        ("GET", "/email/campaigns", None),
        ("POST", "/email/campaigns", {"name": "x"}),
        ("GET", f"/email/campaigns/{campaign}", None),
        ("PATCH", f"/email/campaigns/{campaign}", {"name": "taken"}),
        ("POST", f"/email/campaigns/{campaign}/duplicate", None),
        ("DELETE", f"/email/campaigns/{campaign}", None),
        ("GET", f"/email/campaigns/{campaign}/steps", None),
        ("GET", f"/email/campaigns/{campaign}/steps/impact", None),
        ("POST", f"/email/campaigns/{campaign}/steps", {"name": "x", "subject_template": "s"}),
        ("POST", f"/email/campaigns/{campaign}/steps/reorder", {"step_ids": [step]}),
        ("PATCH", f"/email/steps/{step}", {"body_template": "taken"}),
        ("DELETE", f"/email/steps/{step}", None),
        ("POST", f"/email/steps/{step}/duplicate", None),
        ("POST", f"/email/steps/{step}/ai-variables", {"key": "x", "prompt_template": "y"}),
        ("PATCH", f"/email/ai-variables/{variable}", {"prompt_template": "taken"}),
        ("DELETE", f"/email/ai-variables/{variable}", None),
        (
            "POST",
            "/email/ai-variables/test",
            {"client_profile_id": s.profile_id, "key": "x", "prompt_template": "y"},
        ),
        (
            "POST",
            "/email/templates/preview",
            {"client_profile_id": s.profile_id, "subject_template": "Hi", "body_template": "Hi"},
        ),
        ("GET", "/email/members", None),
        ("GET", f"/email/members/{member}", None),
        ("POST", "/email/members/pause", {"ids": [member]}),
        ("POST", "/email/members/resume", {"ids": [member]}),
        ("POST", "/email/members/remove", {"ids": [member]}),
        ("POST", "/email/members/move", {"ids": [member], "target_campaign_id": campaign}),
        ("POST", f"/email/campaigns/{campaign}/sync", {}),
        ("GET", "/email/outreach?start=2026-10-01&end=2026-10-05", None),
        ("GET", f"/email/campaigns/{campaign}/performance", None),
        ("GET", f"/email/campaigns/{campaign}/launch", None),
        ("POST", f"/email/campaigns/{campaign}/launch", {}),
        ("GET", f"/email/client-profiles/{s.profile_id}", None),
        ("GET", "/email/candidates/client-profiles", None),
        ("GET", "/email/candidates/companies", None),
        (
            "POST",
            f"/email/campaigns/{campaign}/members/preview-add",
            {"client_profile_ids": [s.profile_id]},
        ),
        ("POST", f"/email/campaigns/{campaign}/members/add", {"client_profile_ids": []}),
        ("GET", "/email/import/lemlist", None),
        ("POST", "/email/import/lemlist", {"campaign_ids": ["cam_x"]}),
    ]


def _template(path: str, s: Side) -> str:
    """A concrete path back to its route template."""
    return (
        path.split("?")[0]
        .replace(s.campaign_id, "{campaign_id}")
        .replace(s.step_id, "{step_id}")
        .replace(s.ai_variable_id, "{variable_id}")
        .replace(s.member_id, "{member_id}")
        .replace(s.profile_id, "{client_profile_id}")
    )


def _random_side() -> Side:
    def new() -> str:
        return str(uuid.uuid4())

    return Side({}, uuid.uuid4(), new(), new(), new(), new(), new())


# ── bindings and the boot check ──────────────────────────────────────────────


def test_every_agent_route_is_bound_to_its_permission() -> None:
    assert {(b.method, b.path): b.permission for b in BINDINGS} == EXPECTED_BINDINGS
    assert {b.audience for b in BINDINGS} == {Audience.AGENT}
    collected = {(b.method, b.path) for b in collect_bindings()}
    assert collected >= set(EXPECTED_BINDINGS)
    side = _random_side()
    assert {(m, _template(p, side)) for m, p, _b in _routes(side)} == set(EXPECTED_BINDINGS)


async def test_the_boot_check_passes_with_the_agent_routes(db_session: AsyncSession) -> None:
    from src.main import app

    declared = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", None) or ()
    }
    assert declared >= set(EXPECTED_BINDINGS)  # the router is really included
    await assert_all_routes_bound(app, db_session)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/email/meta"),
        ("PATCH", "/email/steps/{step_id}"),
        ("POST", "/email/members/move"),
    ],
)
async def test_the_boot_check_fails_when_an_agent_route_loses_its_binding(
    db_session: AsyncSession, method: str, path: str
) -> None:
    from src.main import app

    await db_session.execute(
        delete(ProtectedResource).where(
            ProtectedResource.method == method, ProtectedResource.route == path
        )
    )
    await db_session.commit()
    with pytest.raises(StartupError, match="/email/"):
        await assert_all_routes_bound(app, db_session)


# ── lists ────────────────────────────────────────────────────────────────────


async def test_each_agency_lists_only_its_own(client: AsyncClient, both: Both) -> None:
    for here, there in ((both.here, both.there), (both.there, both.here)):
        campaigns = (await client.get("/email/campaigns", headers=here.headers)).json()
        assert [c["id"] for c in campaigns] == [here.campaign_id]
        members = (await client.get("/email/members", headers=here.headers)).json()
        assert [m["id"] for m in members["items"]] == [here.member_id]
        assert members["total"] == 1
        # Filtering on the other agency's campaign finds nothing here.
        filtered = (
            await client.get(
                "/email/members", params={"campaign_id": there.campaign_id}, headers=here.headers
            )
        ).json()
        assert filtered["items"] == [] and filtered["status_counts"] == {}
        # Searching the other agency's client finds nothing either.
        searched = (
            await client.get("/email/members", params={"search": "pedro"}, headers=here.headers)
        ).json()
        assert [m["id"] for m in searched["items"]] == (
            [here.member_id] if here is both.there else []
        )


# ── ids of the other agency ──────────────────────────────────────────────────


async def test_the_other_agencys_ids_are_not_found(
    client: AsyncClient, db_session: AsyncSession, both: Both
) -> None:
    here, there = both.here, both.there
    campaign = there.campaign_id
    calls = [
        ("GET", f"/email/campaigns/{campaign}", None, "email.campaign_not_found"),
        ("PATCH", f"/email/campaigns/{campaign}", {"name": "taken"}, "email.campaign_not_found"),
        ("POST", f"/email/campaigns/{campaign}/duplicate", None, "email.campaign_not_found"),
        ("DELETE", f"/email/campaigns/{campaign}", None, "email.campaign_not_found"),
        ("GET", f"/email/campaigns/{campaign}/steps", None, "email.campaign_not_found"),
        ("GET", f"/email/campaigns/{campaign}/steps/impact", None, "email.campaign_not_found"),
        (
            "POST",
            f"/email/campaigns/{campaign}/steps",
            {"name": "x", "subject_template": "s"},
            "email.campaign_not_found",
        ),
        (
            "POST",
            f"/email/campaigns/{campaign}/steps/reorder",
            {"step_ids": [there.step_id]},
            "email.campaign_not_found",
        ),
        (
            "PATCH",
            f"/email/steps/{there.step_id}",
            {"body_template": "taken"},
            "email.step_not_found",
        ),
        ("DELETE", f"/email/steps/{there.step_id}", None, "email.step_not_found"),
        ("POST", f"/email/steps/{there.step_id}/duplicate", None, "email.step_not_found"),
        (
            "POST",
            f"/email/steps/{there.step_id}/ai-variables",
            {"key": "x", "prompt_template": "y"},
            "email.step_not_found",
        ),
        (
            "PATCH",
            f"/email/ai-variables/{there.ai_variable_id}",
            {"prompt_template": "taken"},
            "email.ai_variable_not_found",
        ),
        (
            "DELETE",
            f"/email/ai-variables/{there.ai_variable_id}",
            None,
            "email.ai_variable_not_found",
        ),
        ("GET", f"/email/members/{there.member_id}", None, "email.member_not_found"),
        (
            "GET",
            f"/email/variables/fill?campaign_id={campaign}",
            None,
            "email.campaign_not_found",
        ),
        # phase 3B
        ("POST", f"/email/campaigns/{campaign}/sync", {}, "email.campaign_not_found"),
        ("POST", f"/email/campaigns/{campaign}/launch", {}, "email.campaign_not_found"),
        ("GET", f"/email/campaigns/{campaign}/launch", None, "email.campaign_not_found"),
        ("GET", f"/email/campaigns/{campaign}/performance", None, "email.campaign_not_found"),
        (
            "POST",
            f"/email/campaigns/{campaign}/members/preview-add",
            {"client_profile_ids": [here.profile_id]},
            "email.campaign_not_found",
        ),
        (
            "POST",
            f"/email/campaigns/{campaign}/members/add",
            {"client_profile_ids": [here.profile_id]},
            "email.campaign_not_found",
        ),
        ("GET", f"/email/client-profiles/{there.profile_id}", None, "profile.not_found"),
        (
            "POST",
            f"/email/campaigns/{here.campaign_id}/members/preview-add",
            {"client_profile_ids": [there.profile_id]},
            "profile.not_found",
        ),
        (
            "POST",
            f"/email/campaigns/{here.campaign_id}/members/add",
            {"client_profile_ids": [there.profile_id]},
            "profile.not_found",
        ),
    ]
    for method, path, body, code in calls:
        response = await client.request(method, path, json=body, headers=here.headers)
        assert response.status_code == 404, f"{method} {path}: {response.status_code}"
        assert response.json()["code"] == code, (method, path)
    # Nothing of the other side changed through this agency's token.
    detail = (await client.get(f"/email/campaigns/{campaign}", headers=there.headers)).json()
    assert detail["name"] == "Pedro campaign" and len(detail["steps"]) == 1
    assert detail["steps"][0]["body_template"] != "taken"
    assert detail["steps"][0]["ai_variables"][0]["prompt_template"] != "taken"
    assert await db_session.scalar(
        select(EmailCampaignAiVariable.id).where(
            EmailCampaignAiVariable.id == uuid.UUID(there.ai_variable_id)
        )
    )

    # Bulk actions name memberships: the other agency's are not found, row by row.
    for action, extra in (
        ("pause", {}),
        ("resume", {}),
        ("remove", {}),
        ("move", {"target_campaign_id": here.campaign_id}),
    ):
        result = (
            await client.post(
                f"/email/members/{action}",
                json={"ids": [there.member_id, here.member_id], **extra},
                headers=here.headers,
            )
        ).json()
        rows = {row["id"]: row for row in result["rows"]}
        assert rows[there.member_id]["ok"] is False, action
        assert rows[there.member_id]["code"] == "email.not_found", action
        assert [row["id"] for row in result["rows"]] == [there.member_id, here.member_id]
    status = await db_session.scalar(
        select(EmailCampaignMember.status)
        .where(EmailCampaignMember.id == uuid.UUID(there.member_id))
        .execution_options(populate_existing=True)
    )
    assert status == "EMAIL_READY"
    # Nobody of the other agency was added here, nor there.
    for side in (here, there):
        members = await db_session.scalars(
            select(EmailCampaignMember.client_profile_id)
            .where(EmailCampaignMember.campaign_id == uuid.UUID(side.campaign_id))
            .execution_options(populate_existing=True)
        )
        assert [str(m) for m in members.all()] == [side.profile_id]


async def test_a_sync_or_a_launch_of_another_agencys_campaign_reaches_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    both: Both,
    fake_email_provider: FakeProvider,
) -> None:
    """A Sync (the reconciliation an agent asks for) or a Launch of the other
    agency's campaign is refused before anything reaches Lemlist."""
    here, there = both.here, both.there
    await set_status(client, there.headers, there.campaign_id, "ACTIVE")
    for action in ("sync", "launch"):
        refused = await client.post(
            f"/email/campaigns/{there.campaign_id}/{action}", json={}, headers=here.headers
        )
        assert refused.status_code == 404 and refused.json()["code"] == "email.campaign_not_found"
    assert fake_email_provider.ops == []
    campaign = await db_session.scalar(
        select(EmailCampaign)
        .where(EmailCampaign.id == uuid.UUID(there.campaign_id))
        .execution_options(populate_existing=True)
    )
    assert campaign is not None and campaign.provider_campaign_id is None
    # Its own agency launches it: the lead reaches Lemlist, once.
    launched = await client.post(
        f"/email/campaigns/{there.campaign_id}/launch", json={}, headers=there.headers
    )
    assert launched.status_code == 200 and launched.json()["members_pushed"] == 1
    assert fake_email_provider.names().count("add_contact") == 1


async def test_previews_and_ai_tests_stay_in_their_agency(client: AsyncClient, both: Both) -> None:
    here, there = both.here, both.there
    body = {"subject_template": "Hi", "body_template": "{{client.first_name}}"}
    for request, code in (
        ({**body, "client_profile_id": there.profile_id}, "profile.not_found"),
        (
            {**body, "client_profile_id": here.profile_id, "step_id": there.step_id},
            "email.step_not_found",
        ),
    ):
        refused = await client.post("/email/templates/preview", json=request, headers=here.headers)
        assert refused.status_code == 404, request
        assert refused.json()["code"] == code
    test = await client.post(
        "/email/ai-variables/test",
        json={"client_profile_id": there.profile_id, "key": "x", "prompt_template": "y"},
        headers=here.headers,
    )
    assert test.status_code == 404 and test.json()["code"] == "profile.not_found"
    # The agency's own client on its own step is fine.
    allowed = await client.post(
        "/email/templates/preview",
        json={**body, "client_profile_id": here.profile_id, "step_id": here.step_id},
        headers=here.headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["body"] == "Lisa"


async def test_moving_never_crosses_agencies(
    client: AsyncClient, db_session: AsyncSession, both: Both
) -> None:
    for here, there in ((both.here, both.there), (both.there, both.here)):
        moved = await client.post(
            "/email/members/move",
            json={"ids": [here.member_id], "target_campaign_id": there.campaign_id},
            headers=here.headers,
        )
        assert moved.status_code == 404
        assert moved.json()["code"] == "email.campaign_not_found"
    for side in (both.here, both.there):
        rows = await db_session.execute(
            select(EmailCampaignMember.status, EmailCampaignMember.campaign_id)
            .where(EmailCampaignMember.agency_id == side.agency_id)
            .execution_options(populate_existing=True)
        )
        assert [(s, str(c)) for s, c in rows.all()] == [("EMAIL_READY", side.campaign_id)]


# ── one permission at a time ─────────────────────────────────────────────────


async def test_each_route_needs_its_own_permission(
    client: AsyncClient,
    email_world: EmailWorld,
    both: Both,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    """A reader (email.view and case.view) reads every view route and is
    refused every other one with the binding's key; without email.view,
    every route is refused."""
    side = both.here
    reader = await role_headers(make_role, make_agent, agent_headers, side.agency_id, *READ)
    blind = await role_headers(
        make_role, make_agent, agent_headers, side.agency_id, Permission.CASE_VIEW
    )
    for method, path, body in _routes(side):
        permission = EXPECTED_BINDINGS[(method, _template(path, side))]
        response = await client.request(method, path, json=body, headers=reader)
        if permission is VIEW:
            assert response.status_code == 200, (method, path, response.text)
        else:
            assert response.status_code == 403, (method, path, response.status_code)
            assert response.json()["code"] == "permission.denied"
            assert response.json()["params"] == {"permission": permission.value}, (method, path)
        refused = await client.request(method, path, json=body, headers=blind)
        assert refused.status_code == 403, (method, path)
        assert refused.json()["params"] == {"permission": permission.value}


async def test_case_view_is_required_next_to_the_email_keys(
    client: AsyncClient,
    both: Both,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    """The module lists clients' names and addresses: every email.* key and
    no case.view opens nothing (spec 4.2)."""
    side = both.here
    no_cases = await role_headers(make_role, make_agent, agent_headers, side.agency_id, *ALL_EMAIL)
    for method, path, body in _routes(side):
        response = await client.request(method, path, json=body, headers=no_cases)
        assert response.status_code == 403, (method, path, response.status_code)
        assert response.json()["code"] == "permission.denied"
        assert response.json()["params"] == {"permission": "case.view"}, (method, path)


async def test_each_email_permission_gates_its_own_action(
    client: AsyncClient,
    db_session: AsyncSession,
    both: Both,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    side = both.here

    async def holder(permission: Permission) -> dict[str, str]:
        return await role_headers(
            make_role, make_agent, agent_headers, side.agency_id, *READ, permission
        )

    editor = await holder(Permission.EMAIL_MANAGE)
    pauser = await holder(Permission.EMAIL_PAUSE)
    remover = await holder(Permission.EMAIL_REMOVE)
    enroller = await holder(Permission.EMAIL_ENROLL)
    syncer = await holder(Permission.EMAIL_SYNC)

    create = {"name": "e"}
    assert (await client.post("/email/campaigns", json=create, headers=editor)).status_code == 201
    for other in (pauser, remover, enroller, syncer):
        assert (
            await client.post("/email/campaigns", json=create, headers=other)
        ).status_code == 403

    ids = {"ids": [side.member_id]}
    for action, allowed in (("pause", pauser), ("resume", pauser), ("remove", remover)):
        for headers in (editor, pauser, remover, enroller):
            response = await client.post(f"/email/members/{action}", json=ids, headers=headers)
            assert response.status_code == (200 if headers is allowed else 403), (action, headers)
    target = await campaign_via_api(client, editor, "Target")
    move = {**ids, "target_campaign_id": target["id"]}
    assert (await client.post("/email/members/move", json=move, headers=pauser)).status_code == 403
    moved = await client.post("/email/members/move", json=move, headers=enroller)
    assert moved.status_code == 200
    # Removed just above: a removed membership moves into a new, ready one.
    assert moved.json()["rows"][0]["ok"] is True, moved.json()

    # Adding is enrolling: email.enroll, and that alone.
    add = f"/email/campaigns/{target['id']}/members/add"
    body = {"client_profile_ids": []}
    for headers, expected in ((editor, 403), (syncer, 403), (pauser, 403), (enroller, 200)):
        assert (await client.post(add, json=body, headers=headers)).status_code == expected
    # Syncing and launching reach Lemlist: email.sync, and that alone.
    await step_via_api(client, editor, target["id"])
    await set_status(client, editor, target["id"], "ACTIVE")
    for action in ("sync", "launch"):
        url = f"/email/campaigns/{target['id']}/{action}"
        for headers in (editor, enroller, pauser, remover):
            refused = await client.post(url, json={}, headers=headers)
            assert refused.status_code == 403, (action, refused.text)
            assert refused.json()["params"] == {"permission": "email.sync"}
        allowed = await client.post(url, json={}, headers=syncer)
        assert allowed.status_code == 200, (action, allowed.text)
        # A sync never hands a lead over; only the launch does.
        assert allowed.json()["members_pushed"] == (1 if action == "launch" else 0)


async def test_every_row_is_checked_even_when_a_binding_is_loosened(
    client: AsyncClient,
    db_session: AsyncSession,
    both: Both,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    """Bindings are data. Were a bulk route's binding loosened to email.view,
    the manager would still refuse each row its own permission (HTTP 200,
    `permission.denied` per row), like Prism's per-row check."""
    side = both.here
    view_id = await db_session.scalar(
        select(PermissionRow.id).where(PermissionRow.key == "email.view")
    )
    for path in ("/email/members/pause", "/email/members/remove", "/email/members/move"):
        await db_session.execute(
            update(ProtectedResource)
            .where(ProtectedResource.method == "POST", ProtectedResource.route == path)
            .values(permission_id=view_id)
        )
    await db_session.commit()
    reader = await role_headers(make_role, make_agent, agent_headers, side.agency_id, *READ)
    for action, permission in (("pause", "email.pause"), ("remove", "email.remove")):
        result = await client.post(
            f"/email/members/{action}", json={"ids": [side.member_id]}, headers=reader
        )
        assert result.status_code == 200, (action, result.text)
        [row] = result.json()["rows"]
        assert (row["ok"], row["code"], row["params"]) == (
            False,
            "permission.denied",
            {"permission": permission},
        ), action
    # Moving checks the target first: the whole request is refused.
    moved = await client.post(
        "/email/members/move",
        json={"ids": [side.member_id], "target_campaign_id": side.campaign_id},
        headers=reader,
    )
    assert moved.status_code == 403 and moved.json()["params"] == {"permission": "email.enroll"}
    status = await db_session.scalar(
        select(EmailCampaignMember.status)
        .where(EmailCampaignMember.id == uuid.UUID(side.member_id))
        .execution_options(populate_existing=True)
    )
    assert status == "EMAIL_READY"


async def test_a_reader_reads_and_acts_on_nothing(
    client: AsyncClient,
    both: Both,
    make_role: MakeRole,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    side = both.here
    reader = await role_headers(make_role, make_agent, agent_headers, side.agency_id, *READ)
    meta = (await client.get("/email/meta", headers=reader)).json()
    flags = ("can_manage", "can_enroll", "can_pause", "can_remove", "can_sync")
    assert [meta[f] for f in flags] == [False] * 5
    detail = (await client.get(f"/email/members/{side.member_id}", headers=reader)).json()
    assert detail["full_name"] == "Lisa Martin" and detail["email"] == "lisa@x.io"


# ── the module's switch, the token ───────────────────────────────────────────


@pytest.mark.parametrize("switch", ["agency_row", "master"])
async def test_the_module_switched_off_answers_409_on_every_route(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    switch: str,
) -> None:
    from shared.models.agency_email_module import AgencyEmailModule

    side = _random_side()
    if switch == "agency_row":
        await db_session.execute(
            update(AgencyEmailModule)
            .where(AgencyEmailModule.agency_id == email_world.agency_id)
            .values(enabled=False)
        )
        await db_session.commit()
    with email_env(EMAIL_MODULE_ENABLED="false" if switch == "master" else "true"):
        for method, path, body in _routes(side):
            response = await client.request(method, path, json=body, headers=email_world.headers)
            assert response.status_code == 409, (switch, method, path, response.status_code)
            assert response.json()["code"] == "email.module_disabled", (method, path)
        # The other agency's row is on: only the master switch closes it too.
        other = await client.get("/email/meta", headers=email_world.other_headers)
        assert other.status_code == (200 if switch == "agency_row" else 409)


async def test_an_agency_without_a_row_has_no_module(
    client: AsyncClient,
    email_module_on: None,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Any],
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    response = await client.get("/email/meta", headers=agent_headers(admin))
    assert response.status_code == 409 and response.json()["code"] == "email.module_disabled"


async def test_an_expat_token_or_none_is_refused_on_every_route(
    client: AsyncClient,
    email_world: EmailWorld,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    expat = await make_expat_user()
    wrong_audience = expat_headers(expat)
    for method, path, body in _routes(_random_side()):
        for headers in (wrong_audience, {}):
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status_code == 401, (method, path, response.status_code)
            assert response.json()["code"] == "auth.session_expired"


# ── impersonation (D6) ───────────────────────────────────────────────────────


async def test_the_launch_and_the_sync_are_refused_under_impersonation(
    client: AsyncClient,
    db_session: AsyncSession,
    email_world: EmailWorld,
    both: Both,
    fake_email_provider: FakeProvider,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Any],
) -> None:
    """Under a mask, the Launch and the Sync would send real emails in the
    client's name: refused whatever the target may do (an admin here), and
    nothing reaches Lemlist. Reading stays open, the other gestures too."""
    side = both.here
    await set_status(client, side.headers, side.campaign_id, "ACTIVE")
    target = await make_agent(agency_id=side.agency_id, role=system_roles["admin"])
    issued = await client.post(
        f"/agencies/me/members/{target.id}/impersonate", headers=email_world.headers
    )
    assert issued.status_code == 200, issued.text
    mask = {"Authorization": f"Bearer {issued.json()['access_token']}"}
    for action in ("launch", "sync"):
        refused = await client.post(
            f"/email/campaigns/{side.campaign_id}/{action}", json={}, headers=mask
        )
        assert refused.status_code == 403, (action, refused.text)
        assert refused.json()["code"] == "impersonation.denied"
    assert fake_email_provider.ops == []
    for path in (
        f"/email/campaigns/{side.campaign_id}/launch",
        f"/email/campaigns/{side.campaign_id}/performance",
        f"/email/client-profiles/{side.profile_id}",
    ):
        assert (await client.get(path, headers=mask)).status_code == 200, path
    preview = await client.post(
        f"/email/campaigns/{side.campaign_id}/members/preview-add",
        json={"client_profile_ids": [side.profile_id]},
        headers=mask,
    )
    assert preview.status_code == 200
    # The same agent, unmasked, launches.
    real = await client.post(
        f"/email/campaigns/{side.campaign_id}/launch", json={}, headers=agent_headers(target)
    )
    assert real.status_code == 200 and real.json()["members_pushed"] == 1


def test_the_denylist_names_the_launch_and_the_sync() -> None:
    from src.core.rbac.enforcement import IMPERSONATION_AGENT_DENIED

    email_denied = {entry for entry in IMPERSONATION_AGENT_DENIED if entry[1].startswith("/email")}
    assert email_denied == IMPERSONATION_DENIED
    assert set(EXPECTED_BINDINGS) >= IMPERSONATION_DENIED
