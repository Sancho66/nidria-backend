"""Activating the Email module for an agency: the platform routes and the
`email_enabled` flag of the agency fiche.

Platform routes (superadmin only, `agency.create`):
    GET    /agencies/{agency_id}/email-module
    PATCH  /agencies/{agency_id}/email-module        {enabled, pause_campaigns}
    POST   /agencies/{agency_id}/email-module/pause-all
    PUT    /agencies/{agency_id}/email-module/team
    DELETE /agencies/{agency_id}/email-module/team

Every Lemlist answer comes from the `GET /team` double (httpx.MockTransport):
the network guard stays at zero for the whole file.
"""

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agency_email_module import AgencyEmailModule
from shared.models.agent import Agent
from shared.models.email_campaign import EmailCampaign
from shared.models.rbac import Role
from src.email_campaigns.providers.base import ProviderError
from tests.email_fakes import FakeProvider
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.email_plugin import (
    FAKE_LEMLIST_KEY,
    TEAM_A,
    TEAM_B,
    LemlistTeamDouble,
    MakeAgencyEmailModule,
    MakeEmailCampaign,
    email_env,
)

pytestmark = pytest.mark.usefixtures("rbac_baseline")

ROUTES = [
    ("GET", "/agencies/{id}/email-module", None),
    ("PATCH", "/agencies/{id}/email-module", {"enabled": True}),
    ("POST", "/agencies/{id}/email-module/pause-all", None),
    ("PUT", "/agencies/{id}/email-module/team", None),
    ("DELETE", "/agencies/{id}/email-module/team", None),
]


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"])


@pytest_asyncio.fixture
async def target(make_agency: MakeAgency) -> Agency:
    return await make_agency(name="Relocation Lisboa")


def _call(
    client: AsyncClient, method: str, path: str, headers: dict[str, str], body: Any = None
) -> Any:
    return client.request(method, path, headers=headers, json=body)


async def _module_row(db: AsyncSession, agency_id: uuid.UUID) -> AgencyEmailModule | None:
    db.expire_all()
    return (
        await db.execute(select(AgencyEmailModule).where(AgencyEmailModule.agency_id == agency_id))
    ).scalar_one_or_none()


# --- reserved to the superadmin -----------------------------------------------------------


@pytest.mark.parametrize("role_name", ["admin", "case_manager", "member", "viewer"])
async def test_every_agency_role_is_refused_on_every_platform_route(
    client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    role_name: str,
) -> None:
    """An agency admin, even of the very agency targeted, never reaches them."""
    agent = await make_agent(role=system_roles[role_name])
    headers = agent_headers(agent)
    for method, path, body in ROUTES:
        response = await _call(client, method, path.format(id=agent.agency_id), headers, body)
        assert response.status_code == 403, (role_name, method, path, response.text)


async def test_the_platform_routes_need_a_token(client: AsyncClient, target: Agency) -> None:
    for method, path, body in ROUTES:
        response = await _call(client, method, path.format(id=target.id), {}, body)
        assert response.status_code == 401, (method, path)


async def test_an_unknown_agency_is_a_404(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(superadmin)
    for method, path, body in ROUTES:
        response = await _call(client, method, path.format(id=uuid.uuid4()), headers, body)
        assert response.status_code == 404, (method, path)
        assert response.json()["code"] == "agency.not_found"


# --- GET: the state ---------------------------------------------------------------------------


async def test_an_agency_without_a_row_is_off_and_dry_run(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    response = await client.get(
        f"/agencies/{target.id}/email-module", headers=agent_headers(superadmin)
    )
    assert response.status_code == 200
    assert response.json() == {
        "agency_id": str(target.id),
        "enabled": False,
        "master_enabled": False,
        "effective": False,
        "external_effects": False,
        "provider": "LEMLIST",
        "team": None,
        "provider_mode": "DRY_RUN",
        "team_mismatch": False,
        "enabled_at": None,
        "disabled_at": None,
        "changed_by_agent_id": None,
        "campaigns": {
            "draft": 0,
            "active": 0,
            "paused": 0,
            "archived": 0,
            "live_at_provider": 0,
        },
    }
    assert await _module_row(db_session, target.id) is None  # a read writes nothing
    assert lemlist_team_double.requests == []


async def test_the_state_counts_the_agency_campaigns_only(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    make_agency: MakeAgency,
    make_email_campaign: MakeEmailCampaign,
) -> None:
    await make_email_campaign(agency_id=target.id)
    await make_email_campaign(agency_id=target.id, status="ACTIVE", provider_campaign_id="cam_1")
    await make_email_campaign(agency_id=target.id, status="ACTIVE", provider_campaign_id="dry_c")
    await make_email_campaign(agency_id=target.id, status="ARCHIVED")
    other = await make_agency()
    await make_email_campaign(agency_id=other.id, status="PAUSED")
    response = await client.get(
        f"/agencies/{target.id}/email-module", headers=agent_headers(superadmin)
    )
    assert response.json()["campaigns"] == {
        "draft": 1,
        "active": 2,
        "paused": 0,
        "archived": 1,
        "live_at_provider": 1,
    }


# --- PATCH: the agency's switch -----------------------------------------------------------


async def test_switching_on_then_off_stamps_and_traces_the_superadmin(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    email_module_on: None,
) -> None:
    headers = agent_headers(superadmin)
    url = f"/agencies/{target.id}/email-module"
    on = await client.patch(url, headers=headers, json={"enabled": True})
    assert on.status_code == 200
    body = on.json()
    assert (body["enabled"], body["master_enabled"], body["effective"]) == (True, True, True)
    assert body["changed_by_agent_id"] == str(superadmin.id)
    assert body["enabled_at"] is not None and body["disabled_at"] is None
    assert body["provider_mode"] == "DRY_RUN"  # unbound: nothing leaves

    # Idempotent: switching on again changes nothing, not even the stamp.
    again = await client.patch(url, headers=headers, json={"enabled": True})
    assert again.json()["enabled_at"] == body["enabled_at"]

    off = await client.patch(url, headers=headers, json={"enabled": False})
    assert off.status_code == 200
    assert (off.json()["enabled"], off.json()["effective"]) == (False, False)
    assert off.json()["disabled_at"] is not None
    row = await _module_row(db_session, target.id)
    assert row is not None and row.enabled is False and row.disabled_at is not None


async def test_switching_on_without_the_master_switch_is_stored_but_not_effective(
    client: AsyncClient, superadmin: Agent, agent_headers: AuthHeaders, target: Agency
) -> None:
    response = await client.patch(
        f"/agencies/{target.id}/email-module",
        headers=agent_headers(superadmin),
        json={"enabled": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["enabled"], body["master_enabled"], body["effective"]) == (True, False, False)


async def test_switching_off_an_agency_without_a_row_creates_none(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
) -> None:
    response = await client.patch(
        f"/agencies/{target.id}/email-module",
        headers=agent_headers(superadmin),
        json={"enabled": False},
    )
    assert response.status_code == 200
    assert await _module_row(db_session, target.id) is None


@pytest.mark.parametrize("provider_campaign_id", ["cam_8gtvSu", "dryXreal"])
async def test_switching_off_is_refused_while_a_campaign_runs_at_lemlist(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
    provider_campaign_id: str,
) -> None:
    """`dryXreal` is a REAL id: the `_` of the dry-run prefix is no wildcard."""
    target_id = target.id  # a refusal rolls back: objects expire
    await make_agency_email_module(agency_id=target_id)
    await make_email_campaign(
        agency_id=target_id, status="ACTIVE", provider_campaign_id=provider_campaign_id
    )
    response = await client.patch(
        f"/agencies/{target_id}/email-module",
        headers=agent_headers(superadmin),
        json={"enabled": False},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email.campaigns_live"
    assert response.json()["params"] == {"count": 1}
    row = await _module_row(db_session, target_id)
    assert row is not None and row.enabled is True and row.disabled_at is None


async def test_switching_off_is_allowed_with_dry_run_paused_or_draft_campaigns(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
) -> None:
    await make_agency_email_module(agency_id=target.id)
    await make_email_campaign(agency_id=target.id, status="ACTIVE", provider_campaign_id="dry_c1")
    await make_email_campaign(agency_id=target.id, status="PAUSED", provider_campaign_id="cam_2")
    await make_email_campaign(agency_id=target.id)
    response = await client.patch(
        f"/agencies/{target.id}/email-module",
        headers=agent_headers(superadmin),
        json={"enabled": False},
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False


# --- PUT team: bind the key's team ------------------------------------------------------


async def test_binding_is_refused_without_real_effects_and_without_any_call(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    response = await client.put(
        f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email.provider_unavailable"
    assert response.json()["params"] == {"reason": "not_configured"}
    assert lemlist_team_double.requests == []
    assert await _module_row(db_session, target.id) is None


async def test_binding_is_refused_with_effects_but_without_a_key(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    lemlist_team_double: LemlistTeamDouble,
) -> None:
    with email_env(EMAIL_PROVIDER_EXTERNAL_EFFECTS="true", MOCK_LEMLIST="false"):
        response = await client.put(
            f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
        )
    assert response.status_code == 409
    assert response.json()["params"] == {"reason": "not_configured"}
    assert lemlist_team_double.requests == []


@pytest.mark.parametrize("outage", ["http", "network"])
async def test_binding_is_refused_when_lemlist_does_not_say(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    lemlist_live: LemlistTeamDouble,
    outage: str,
) -> None:
    if outage == "http":
        lemlist_live.status_code = 500
    else:
        lemlist_live.error = httpx.ConnectError("simulated")
    response = await client.put(
        f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email.provider_unavailable"
    assert response.json()["params"] == {"reason": "team_unknown"}
    assert await _module_row(db_session, target.id) is None


async def test_binding_binds_the_team_the_key_opens(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    make_agency_email_module: MakeAgencyEmailModule,
    email_module_on: None,
    lemlist_live: LemlistTeamDouble,
) -> None:
    await make_agency_email_module(agency_id=target.id)
    response = await client.put(
        f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["team"] == {"id": TEAM_A, "name": "Nidria test team"}
    assert (body["provider_mode"], body["team_mismatch"]) == ("LIVE", False)
    assert body["external_effects"] is True
    assert body["changed_by_agent_id"] == str(superadmin.id)
    row = await _module_row(db_session, target.id)
    assert row is not None and row.provider_team_id == TEAM_A
    # Basic auth with an empty login and the key: what Lemlist expects.
    auth = base64.b64encode(f":{FAKE_LEMLIST_KEY}".encode()).decode()
    assert {r.headers["Authorization"] for r in lemlist_live.requests} == {f"Basic {auth}"}


async def test_binding_an_agency_without_a_row_creates_it_switched_off(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    lemlist_live: LemlistTeamDouble,
) -> None:
    response = await client.put(
        f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["provider_mode"] == "DRY_RUN"  # bound, but the module is off
    row = await _module_row(db_session, target.id)
    assert row is not None and row.enabled is False and row.provider_team_id == TEAM_A


async def test_a_team_serves_one_agency(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    make_agency: MakeAgency,
    db_session: AsyncSession,
    make_agency_email_module: MakeAgencyEmailModule,
    lemlist_live: LemlistTeamDouble,
) -> None:
    owner = await make_agency(name="Expat Porto")
    await make_agency_email_module(agency_id=owner.id, provider_team_id=TEAM_A)
    response = await client.put(
        f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email.team_bound_elsewhere"
    assert response.json()["params"] == {"agency_id": str(owner.id), "agency_name": "Expat Porto"}
    assert await _module_row(db_session, target.id) is None


async def test_rebinding_the_same_team_is_a_no_op(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    make_agency_email_module: MakeAgencyEmailModule,
    lemlist_live: LemlistTeamDouble,
) -> None:
    await make_agency_email_module(agency_id=target.id, provider_team_id=TEAM_A)
    response = await client.put(
        f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 200
    assert response.json()["team"]["id"] == TEAM_A
    assert response.json()["changed_by_agent_id"] is None  # nothing changed


async def test_moving_to_another_team_is_refused_once_campaigns_exist_at_lemlist(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
    lemlist_live: LemlistTeamDouble,
) -> None:
    target_id = target.id  # a refusal rolls back: objects expire
    await make_agency_email_module(agency_id=target_id, provider_team_id=TEAM_B)
    await make_email_campaign(
        agency_id=target_id, status="ARCHIVED", provider_campaign_id="cam_old"
    )
    response = await client.put(
        f"/agencies/{target_id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email.team_has_history"
    assert response.json()["params"] == {"count": 1}
    row = await _module_row(db_session, target_id)
    assert row is not None and row.provider_team_id == TEAM_B


async def test_moving_to_another_team_without_history_is_allowed(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
    lemlist_live: LemlistTeamDouble,
) -> None:
    await make_agency_email_module(agency_id=target.id, provider_team_id=TEAM_B)
    await make_email_campaign(agency_id=target.id, status="ACTIVE", provider_campaign_id="dry_c")
    response = await client.put(
        f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 200
    assert response.json()["team"]["id"] == TEAM_A


# --- DELETE team: unbind -------------------------------------------------------------------


async def test_unbinding_is_refused_once_a_campaign_exists_at_lemlist(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
) -> None:
    target_id = target.id  # a refusal rolls back: objects expire
    await make_agency_email_module(agency_id=target_id, provider_team_id=TEAM_A)
    await make_email_campaign(agency_id=target_id, status="ARCHIVED", provider_campaign_id="cam_1")
    response = await client.delete(
        f"/agencies/{target_id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email.team_has_history"
    assert response.json()["params"] == {"count": 1}
    row = await _module_row(db_session, target_id)
    assert row is not None and row.provider_team_id == TEAM_A


async def test_unbinding_returns_the_agency_to_dry_run(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
) -> None:
    await make_agency_email_module(agency_id=target.id, provider_team_id=TEAM_A)
    await make_email_campaign(agency_id=target.id, status="ACTIVE", provider_campaign_id="dry_c")
    response = await client.delete(
        f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 200
    assert (response.json()["team"], response.json()["provider_mode"]) == (None, "DRY_RUN")
    assert response.json()["changed_by_agent_id"] == str(superadmin.id)
    row = await _module_row(db_session, target.id)
    assert row is not None and row.provider_team_id is None


async def test_unbinding_an_unbound_agency_changes_nothing(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
) -> None:
    response = await client.delete(
        f"/agencies/{target.id}/email-module/team", headers=agent_headers(superadmin)
    )
    assert response.status_code == 200
    assert response.json()["team"] is None
    assert await _module_row(db_session, target.id) is None


# --- live, dry run, unavailable, mismatch ----------------------------------------------------


async def test_a_bound_agency_whose_team_is_unknown_is_unavailable_not_dry_run(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    make_agency_email_module: MakeAgencyEmailModule,
    email_module_on: None,
    lemlist_live: LemlistTeamDouble,
) -> None:
    await make_agency_email_module(agency_id=target.id, provider_team_id=TEAM_A)
    lemlist_live.status_code = 503
    response = await client.get(
        f"/agencies/{target.id}/email-module", headers=agent_headers(superadmin)
    )
    assert response.status_code == 200
    assert response.json()["provider_mode"] == "UNAVAILABLE"


async def test_a_key_opening_another_team_is_a_mismatch(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    make_agency_email_module: MakeAgencyEmailModule,
    email_module_on: None,
    lemlist_live: LemlistTeamDouble,
) -> None:
    await make_agency_email_module(agency_id=target.id, provider_team_id=TEAM_B)
    response = await client.get(
        f"/agencies/{target.id}/email-module", headers=agent_headers(superadmin)
    )
    body = response.json()
    assert (body["provider_mode"], body["team_mismatch"]) == ("DRY_RUN", True)


# --- email_enabled on the agency fiche ---------------------------------------------------


@pytest.mark.parametrize(
    ("master", "row", "expected"),
    [
        ("false", None, False),
        ("false", True, False),
        ("true", None, False),
        ("true", False, False),
        ("true", True, True),
    ],
)
async def test_the_fiche_serves_the_effective_switch(
    client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
    make_agency_email_module: MakeAgencyEmailModule,
    master: str,
    row: bool | None,
    expected: bool,
) -> None:
    """Env master AND the agency's row; any agent of the agency reads it."""
    admin = await make_agent(role=system_roles["admin"])
    viewer = await make_agent(role=system_roles["viewer"], agency_id=admin.agency_id)
    if row is not None:
        await make_agency_email_module(agency_id=admin.agency_id, enabled=row)
    with email_env(EMAIL_MODULE_ENABLED=master):
        me = await client.get("/agencies/me", headers=agent_headers(viewer))
        patched = await client.patch(
            "/agencies/me", headers=agent_headers(admin), json={"name": "Renamed agency"}
        )
        logo = await client.delete("/agencies/me/logo", headers=agent_headers(admin))
        cover = await client.delete("/agencies/me/cover", headers=agent_headers(admin))
    for response in (me, patched, logo, cover):
        assert response.status_code == 200, response.text
        assert response.json()["email_enabled"] is expected, response.request.url


async def test_the_platform_agency_list_serves_each_agency_switch(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    make_agency: MakeAgency,
    make_agency_email_module: MakeAgencyEmailModule,
    email_module_on: None,
) -> None:
    off = await make_agency(name="Switched off")
    await make_agency_email_module(agency_id=target.id)
    await make_agency_email_module(agency_id=off.id, enabled=False)
    response = await client.get("/agencies", headers=agent_headers(superadmin))
    assert response.status_code == 200
    flags = {item["id"]: item["email_enabled"] for item in response.json()}
    assert flags[str(target.id)] is True
    assert flags[str(off.id)] is False
    assert flags[str(superadmin.agency_id)] is False


# --- POST pause-all: stop an agency's sends (decision of 08/10/2026) ---------------------


async def _statuses(db: AsyncSession, agency_id: uuid.UUID) -> dict[str, str]:
    rows = await db.execute(
        select(EmailCampaign.name, EmailCampaign.status)
        .where(EmailCampaign.agency_id == agency_id)
        .execution_options(populate_existing=True)
    )
    return dict(rows.tuples().all())


async def _campaigns(
    make_email_campaign: MakeEmailCampaign,
    agency_id: uuid.UUID,
    *specs: tuple[str, str, str | None, str | None],
) -> dict[str, uuid.UUID]:
    """(name, status, provider campaign id, Lemlist team) -> campaign ids."""
    made = {}
    for name, status, provider_id, team in specs:
        campaign = await make_email_campaign(
            agency_id=agency_id,
            name=name,
            status=status,
            provider_campaign_id=provider_id,
            provider_metadata={"team_id": team} if team else {},
        )
        made[name] = campaign.id
    return made


async def test_pause_all_pauses_every_active_campaign_at_lemlist_and_in_nidria(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
    fake_email_provider: FakeProvider,
) -> None:
    """At Lemlist through the agency's provider for a campaign that lives
    there (never another team's: its ids are not the current team's), in
    Nidria only for a dry run's or one never synced. One commit per
    campaign: a failure is told with its code, the others go on. Imported
    campaigns are ACTIVE campaigns like any other (no filter on origin)."""
    # Plain values: a failed pause rolls the shared session back.
    target_id, headers = target.id, agent_headers(superadmin)
    await make_agency_email_module(agency_id=target_id)
    ids = await _campaigns(
        make_email_campaign,
        target_id,
        ("Live", "ACTIVE", "cam_live", "tea_current"),
        ("Dry", "ACTIVE", "dry_cam_1", None),
        ("Never synced", "ACTIVE", None, None),
        ("Other team", "ACTIVE", "cam_other", "tea_other"),
        ("Broken", "ACTIVE", "cam_broken", "tea_current"),
        ("Paused", "PAUSED", "cam_paused", "tea_current"),
        ("Draft", "DRAFT", None, None),
        ("Archived", "ARCHIVED", "cam_archived", "tea_current"),
    )
    elsewhere_id = (await make_agency(name="Elsewhere")).id
    await make_email_campaign(
        agency_id=elsewhere_id,
        name="Theirs",
        status="ACTIVE",
        provider_campaign_id="cam_theirs",
        provider_metadata={"team_id": "tea_current"},
    )
    fake_email_provider.failures[("pause_campaign", "cam_broken")] = ProviderError(
        "Lemlist did not answer in time.", code="TIMEOUT"
    )
    url = f"/agencies/{target_id}/email-module/pause-all"
    first = await client.post(url, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["paused"], body["already_paused"]) == (3, 3)
    assert body["failed"] == [
        {"campaign_id": str(ids["Other team"]), "code": "email.team_mismatch", "params": {}},
        {"campaign_id": str(ids["Broken"]), "code": "email.provider_unreachable", "params": {}},
    ]
    assert body["module"]["campaigns"]["active"] == 2
    assert body["module"]["campaigns"]["paused"] == 4
    assert fake_email_provider.ops == [
        ("pause_campaign", {"campaign": "cam_live"}),
        ("pause_campaign", {"campaign": "cam_broken"}),
    ]
    statuses = await _statuses(db_session, target_id)
    assert statuses == {
        "Live": "PAUSED",
        "Dry": "PAUSED",
        "Never synced": "PAUSED",
        "Other team": "ACTIVE",
        "Broken": "ACTIVE",
        "Paused": "PAUSED",
        "Draft": "DRAFT",
        "Archived": "ARCHIVED",
    }
    assert (await _statuses(db_session, elsewhere_id)) == {"Theirs": "ACTIVE"}

    # Idempotent: again, Lemlist answering now; another team's stays as it is.
    fake_email_provider.failures.clear()
    fake_email_provider.ops.clear()
    second = (await client.post(url, headers=headers)).json()
    assert (second["paused"], second["already_paused"]) == (1, 6)
    assert [f["code"] for f in second["failed"]] == ["email.team_mismatch"]
    assert fake_email_provider.ops == [("pause_campaign", {"campaign": "cam_broken"})]
    fake_email_provider.ops.clear()
    third = (await client.post(url, headers=headers)).json()
    assert (third["paused"], third["already_paused"], len(third["failed"])) == (0, 7, 1)
    assert fake_email_provider.ops == []


async def test_pause_all_never_reaches_lemlist_while_the_agency_runs_dry(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
    fake_email_provider: FakeProvider,
) -> None:
    """The agency does not talk to Lemlist for real (the rules of a status
    change): its real campaign stays ACTIVE with the code saying why, its
    dry run's is paused in Nidria."""
    target_id, headers = target.id, agent_headers(superadmin)
    await make_agency_email_module(agency_id=target_id)
    fake_email_provider.external = False
    ids = await _campaigns(
        make_email_campaign,
        target_id,
        ("Live", "ACTIVE", "cam_live", "tea_current"),
        ("Dry", "ACTIVE", "dry_cam_1", None),
    )
    answer = await client.post(f"/agencies/{target_id}/email-module/pause-all", headers=headers)
    assert answer.status_code == 200, answer.text
    assert answer.json()["paused"] == 1
    assert answer.json()["failed"] == [
        {
            "campaign_id": str(ids["Live"]),
            "code": "email.provider_unavailable",
            "params": {"reason": "external_effects_off"},
        }
    ]
    assert fake_email_provider.ops == []
    assert await _statuses(db_session, target_id) == {"Live": "ACTIVE", "Dry": "PAUSED"}


async def test_pause_all_stops_an_agency_under_a_billing_lock(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
    fake_email_provider: FakeProvider,
) -> None:
    """The billing lock reads the ACTOR's agency (the superadmin's, exempt
    anyway): a locked agency is paused all the same (gap E7), while its own
    admin writes nothing."""
    locked = await make_agency(
        name="Trial over", trial_ends_at=datetime.now(UTC) - timedelta(days=2)
    )
    locked_id = locked.id
    await make_agency_email_module(agency_id=locked_id)
    await _campaigns(make_email_campaign, locked_id, ("Live", "ACTIVE", "cam_live", "tea_current"))
    admin = await make_agent(agency_id=locked_id, role=system_roles["admin"])
    refused = await client.patch(
        "/agencies/me", headers=agent_headers(admin), json={"name": "Renamed"}
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "billing.subscription_required"
    answer = await client.post(
        f"/agencies/{locked_id}/email-module/pause-all", headers=agent_headers(superadmin)
    )
    assert answer.status_code == 200, answer.text
    assert (answer.json()["paused"], answer.json()["failed"]) == (1, [])
    assert fake_email_provider.ops == [("pause_campaign", {"campaign": "cam_live"})]
    assert await _statuses(db_session, locked_id) == {"Live": "PAUSED"}


async def test_switching_off_with_pause_campaigns_pauses_them_then_switches_off(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
    fake_email_provider: FakeProvider,
) -> None:
    target_id = target.id
    await make_agency_email_module(agency_id=target_id)
    await _campaigns(
        make_email_campaign,
        target_id,
        ("Live", "ACTIVE", "cam_live", "tea_current"),
        ("Dry", "ACTIVE", "dry_cam_1", None),
    )
    url = f"/agencies/{target_id}/email-module"
    headers = agent_headers(superadmin)
    # The flag is read when switching OFF only.
    kept_on = await client.patch(
        url, headers=headers, json={"enabled": True, "pause_campaigns": True}
    )
    assert kept_on.status_code == 200 and kept_on.json()["enabled"] is True
    assert fake_email_provider.ops == []
    # Without it, a campaign live at Lemlist still refuses the switch-off.
    refused = await client.patch(url, headers=headers, json={"enabled": False})
    assert refused.status_code == 409 and refused.json()["code"] == "email.campaigns_live"
    off = await client.patch(url, headers=headers, json={"enabled": False, "pause_campaigns": True})
    assert off.status_code == 200, off.text
    assert (off.json()["enabled"], off.json()["campaigns"]["live_at_provider"]) == (False, 0)
    assert fake_email_provider.ops == [("pause_campaign", {"campaign": "cam_live"})]
    assert await _statuses(db_session, target_id) == {"Live": "PAUSED", "Dry": "PAUSED"}


async def test_a_campaign_still_live_after_the_pause_keeps_the_module_on(
    client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    target: Agency,
    db_session: AsyncSession,
    make_agency_email_module: MakeAgencyEmailModule,
    make_email_campaign: MakeEmailCampaign,
    fake_email_provider: FakeProvider,
) -> None:
    target_id, headers = target.id, agent_headers(superadmin)
    await make_agency_email_module(agency_id=target_id)
    await _campaigns(
        make_email_campaign,
        target_id,
        ("Live", "ACTIVE", "cam_live", "tea_current"),
        ("Stuck", "ACTIVE", "cam_stuck", "tea_current"),
    )
    fake_email_provider.failures[("pause_campaign", "cam_stuck")] = ProviderError(
        "Lemlist: server error", code="PROVIDER_ERROR", status=500
    )
    refused = await client.patch(
        f"/agencies/{target_id}/email-module",
        headers=headers,
        json={"enabled": False, "pause_campaigns": True},
    )
    assert refused.status_code == 409
    assert (refused.json()["code"], refused.json()["params"]) == (
        "email.campaigns_live",
        {"count": 1},
    )
    row = await _module_row(db_session, target_id)
    assert row is not None and row.enabled is True and row.disabled_at is None
    # What could be paused stays paused.
    assert await _statuses(db_session, target_id) == {"Live": "PAUSED", "Stuck": "ACTIVE"}
