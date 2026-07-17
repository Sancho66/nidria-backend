"""Offboarding d'un agent (deactivated_at, jamais un DELETE).

La coupure est IMMÉDIATE (login refusé, access token vivant tué à la
requête suivante, refresh révoqués), le membre sort de tous les comptages
(sièges ET prestataires), le push Paddle descend (full_next_billing_period,
no-op en manual), l'anti-lockout par capacité protège le dernier manager,
l'inventaire (dossiers, étapes actives) part avec la réponse, et la
réactivation symétrique re-vérifie le cap comme une acceptation."""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.rbac import Role
from tests.plugins.agent_plugin import DEFAULT_PASSWORD, AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post(
        "/auth/agent/login", json={"email": email, "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_deactivation_cuts_access_immediately(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    target = await make_agent(
        agency_id=admin.agency_id, role=system_roles["member"], email="leaver@x.io"
    )
    tokens = await _login(client, "leaver@x.io")
    live = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await client.get("/auth/agent/me", headers=live)).status_code == 200

    resp = await client.post(
        f"/agencies/me/members/{target.id}/deactivate", headers=agent_headers(admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deactivated_at"] is not None

    # The LIVE access token dies at the very next request…
    assert (await client.get("/auth/agent/me", headers=live)).status_code == 401
    # …the login is refused with the NON-REVEALING credentials error…
    denied = await client.post(
        "/auth/agent/login", json={"email": "leaver@x.io", "password": DEFAULT_PASSWORD}
    )
    assert denied.status_code == 401
    # …and the refresh token is revoked.
    refreshed = await client.post(
        "/auth/agent/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 401


async def test_deactivation_pushes_paddle_down_and_manual_is_noop(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.billing import paddle_client
    from src.core.config import get_settings

    aid = admin.agency_id
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    price_ids = {"cabinet_mensuel": "pri_b", "seat_cabinet_mensuel": "pri_s"}
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_PRICE_IDS", json.dumps(price_ids))
    get_settings.cache_clear()
    try:
        # A paddle cabinet agency at 5 members (billed 2).
        await db_session.execute(
            update(Agency)
            .where(Agency.id == aid)
            .values(
                billing_mode="paddle",
                paddle_subscription_id="sub_off",
                plan="cabinet",
                billing_cycle="mensuel",
                converted_at=datetime.now(UTC),
                billing_status="active",
            )
        )
        await db_session.commit()
        extras = [
            await make_agent(agency_id=aid, role=system_roles["member"], email=f"m{i}@x.io")
            for i in range(4)
        ]

        resp = await client.post(
            f"/agencies/me/members/{extras[0].id}/deactivate", headers=agent_headers(admin)
        )
        assert resp.status_code == 200, resp.text
        # Push DOWN: derived quantity 1 (4 actifs − 3 inclus), next cycle.
        push.assert_awaited_once()
        sub_id, kwargs = push.await_args.args[0], push.await_args.kwargs
        assert sub_id == "sub_off"
        assert kwargs["proration_billing_mode"] == "full_next_billing_period"
        seat_item = next(i for i in kwargs["items"] if i["price_id"] == "pri_s")
        assert seat_item["quantity"] == 1

        # MANUAL agency: same gesture, sync NO-OP (zero Paddle call).
        push.reset_mock()
        other_admin = await make_agent(role=system_roles["admin"], email="manual-adm@x.io")
        leaver = await make_agent(
            agency_id=other_admin.agency_id, role=system_roles["member"], email="manual-l@x.io"
        )
        resp = await client.post(
            f"/agencies/me/members/{leaver.id}/deactivate", headers=agent_headers(other_admin)
        )
        assert resp.status_code == 200, resp.text
        push.assert_not_awaited()
        # And the member is OUT of the seat count.
        seats = (await client.get("/agencies/me", headers=agent_headers(other_admin))).json()[
            "subscription"
        ]["seats"]
        assert seats["members"] == 1
    finally:
        get_settings.cache_clear()


async def test_anti_lockout_protects_the_last_manager(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    # Alone with agent.manage: self-deactivation refused.
    blocked = await client.post(f"/agencies/me/members/{admin.id}/deactivate", headers=h)
    assert blocked.status_code == 409, blocked.text

    # A second manager exists: the voluntary offboarding passes.
    await make_agent(agency_id=admin.agency_id, role=system_roles["admin"], email="adm2@x.io")
    allowed = await client.post(f"/agencies/me/members/{admin.id}/deactivate", headers=h)
    assert allowed.status_code == 200, allowed.text


async def test_inventory_lists_owned_cases_and_active_steps(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    target = await make_agent(
        agency_id=admin.agency_id, role=system_roles["member"], email="owner@x.io"
    )
    case = await make_client_case(agency_id=admin.agency_id, owner_agent_id=target.id)
    tid = (await client.post("/journeys", headers=h, json={"name": "T"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=h, json={"name": "Etape"})
    [step] = (
        await client.post(f"/cases/{case.id}/journey", headers=h, json={"journey_template_id": tid})
    ).json()
    await db_session.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.id == step["id"])
        .values(responsible_agent_id=target.id, responsible_type="agent")
    )
    await db_session.commit()

    resp = await client.post(f"/agencies/me/members/{target.id}/deactivate", headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["owned_cases"] == [str(case.id)]
    assert body["responsible_steps"] == [{"case_id": str(case.id), "progress_id": step["id"]}]


async def test_provider_deactivation_frees_the_slot(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    from sqlalchemy import select

    h = agent_headers(admin)
    role = (
        await db_session.execute(select(Role).where(Role.is_external.is_(True)).limit(1))
    ).scalar_one()
    externals = [
        await make_agent(
            agency_id=admin.agency_id,
            role=role,
            is_external=True,
            email=f"prov-off-{i}@x.io",
        )
        for i in range(10)  # the trial cap
    ]
    role_id = str(role.id)
    refused = await client.post(
        "/agencies/me/external-invitations",
        headers=h,
        json={"name": "Onze", "email": "onze@x.io", "role_id": role_id},
    )
    assert refused.status_code == 409

    gone = await client.post(f"/agencies/me/members/{externals[0].id}/deactivate", headers=h)
    assert gone.status_code == 200, gone.text
    retry = await client.post(
        "/agencies/me/external-invitations",
        headers=h,
        json={"name": "Onze bis", "email": "onzebis@x.io", "role_id": role_id},
    )
    assert retry.status_code == 201, retry.text


async def test_reactivation_rechecks_the_cap_then_restores_access(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    target = await make_agent(
        agency_id=admin.agency_id, role=system_roles["member"], email="back@x.io"
    )
    resp = await client.post(f"/agencies/me/members/{target.id}/deactivate", headers=h)
    assert resp.status_code == 200

    # The roster refills to the trial cap (3 actives) while they are away…
    filler = await make_agent(
        agency_id=admin.agency_id, role=system_roles["member"], email="filler@x.io"
    )
    await make_agent(agency_id=admin.agency_id, role=system_roles["member"], email="filler2@x.io")
    # …coming back would overflow: the same rule as accepting an invitation.
    blocked = await client.post(f"/agencies/me/members/{target.id}/reactivate", headers=h)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "subscription.seat_limit"

    # A slot frees up → reactivation passes → the login works again.
    freed = await client.post(f"/agencies/me/members/{filler.id}/deactivate", headers=h)
    assert freed.status_code == 200
    back = await client.post(f"/agencies/me/members/{target.id}/reactivate", headers=h)
    assert back.status_code == 204, back.text
    assert (await _login(client, "back@x.io"))["access_token"]


async def test_deactivated_member_is_not_impersonable_and_badge_served(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    target = await make_agent(
        agency_id=admin.agency_id, role=system_roles["member"], email="ghosted@x.io"
    )
    await client.post(f"/agencies/me/members/{target.id}/deactivate", headers=h)

    # No seat to sit in: the impersonation mint refuses.
    minted = await client.post(f"/agencies/me/members/{target.id}/impersonate", headers=h)
    assert minted.status_code == 404
    # The member STAYS listed, with the badge field for the front.
    members = (await client.get("/agencies/me/members", headers=h)).json()
    row = next(m for m in members if m["email"] == "ghosted@x.io")
    assert row["deactivated_at"] is not None
    # Foreign / unknown targets: non-revealing 404.
    assert (
        await client.post(f"/agencies/me/members/{uuid.uuid4()}/deactivate", headers=h)
    ).status_code == 404
