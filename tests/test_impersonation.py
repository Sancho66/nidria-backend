"""Step 19 battery: impersonation — issuance on both audiences, agency
scoping, no chaining, no refresh, denied surface (auth mutations,
structure mutations, expat writes), target-not-impersonator
permissions, additive sync to admin, audit log, expiry."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from jose import jwt as jose_jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.auth_tokens import RefreshToken
from shared.models.impersonation import ImpersonationLog
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role, RolePermission
from src.core.config import get_settings
from src.core.enums import Audience
from src.core.rbac.baseline import seed_system_roles
from src.core.security import create_access_token
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def imp_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(roles=[system_roles["admin"]], first_name="Alice", last_name="Admin")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _impersonate_agent(
    client: AsyncClient, actor_headers: dict[str, str], target_id: uuid.UUID
) -> str:
    response = await client.post(
        f"/agencies/me/members/{target_id}/impersonate", headers=actor_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["audience"] == "agent"
    assert body["expires_in_minutes"] == 30
    assert "refresh_token" not in body  # expiry IS the exit
    return str(body["access_token"])


# --- issuance, both audiences ------------------------------------------------------


async def test_impersonate_agent_emission_and_me_banner(
    imp_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    target = await make_agent(
        agency_id=admin.agency_id, roles=[system_roles["member"]], first_name="Tom"
    )
    token = await _impersonate_agent(imp_client, agent_headers(admin), target.id)

    me = await imp_client.get("/auth/agent/me", headers=_bearer(token))
    assert me.status_code == 200
    body = me.json()
    assert body["id"] == str(target.id)  # subject is the TARGET
    assert body["impersonator"] == {
        "agent_id": str(admin.id),
        "first_name": "Alice",
        "last_name": "Admin",
    }


async def test_impersonate_expat_emission_and_scoping(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    client_expat = await make_expat_user(first_name="Jean")
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=client_expat.id)
    response = await imp_client.post(
        f"/expat-users/{client_expat.id}/impersonate", headers=agent_headers(admin)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["audience"] == "expat"
    assert "refresh_token" not in body

    me = await imp_client.get("/auth/expat/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["id"] == str(client_expat.id)
    assert me.json()["impersonator"]["agent_id"] == str(admin.id)

    # An expat with no case in the actor's agency: 404, not a master key.
    stranger = await make_expat_user()
    response = await imp_client.post(
        f"/expat-users/{stranger.id}/impersonate", headers=agent_headers(admin)
    )
    assert response.status_code == 404


async def test_self_impersonation_422(
    imp_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await imp_client.post(
        f"/agencies/me/members/{admin.id}/impersonate", headers=agent_headers(admin)
    )
    assert response.status_code == 422


async def test_cross_agency_target_404(
    imp_client: AsyncClient, admin: Agent, make_agent: MakeAgent, agent_headers: AuthHeaders
) -> None:
    foreign_agent = await make_agent()  # own (other) agency
    response = await imp_client.post(
        f"/agencies/me/members/{foreign_agent.id}/impersonate", headers=agent_headers(admin)
    )
    assert response.status_code == 404


# --- permission gate ---------------------------------------------------------------


async def test_without_permission_403_including_case_manager(
    imp_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """case_manager is built BY EXCLUSION in the system matrix — this
    pins that agent.impersonate was added to its exclusion set and not
    silently inherited."""
    for role_name in ("member", "viewer", "case_manager"):
        actor = await make_agent(roles=[system_roles[role_name]])
        colleague = await make_agent(agency_id=actor.agency_id)
        response = await imp_client.post(
            f"/agencies/me/members/{colleague.id}/impersonate",
            headers=agent_headers(actor),
        )
        assert response.status_code == 403, role_name


async def test_additive_sync_grants_impersonate_to_admin(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """The step-7 mechanism: simulate a pre-step-19 deployment by
    removing the pairing, then re-run the seed — the additive sync must
    restore agent.impersonate on the admin system role."""
    perm_id = (
        await db_session.execute(
            select(PermissionRow.id).where(PermissionRow.key == "agent.impersonate")
        )
    ).scalar_one()
    admin_role_id = system_roles["admin"].id
    await db_session.execute(
        delete(RolePermission).where(
            RolePermission.role_id == admin_role_id,
            RolePermission.permission_id == perm_id,
        )
    )
    await db_session.commit()

    await seed_system_roles(db_session)
    await db_session.commit()

    restored = (
        await db_session.execute(
            select(RolePermission).where(
                RolePermission.role_id == admin_role_id,
                RolePermission.permission_id == perm_id,
            )
        )
    ).scalar_one_or_none()
    assert restored is not None


# --- the central security points ----------------------------------------------------


async def test_chaining_403_on_both_endpoints(
    imp_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Even impersonating an ADMIN (who holds agent.impersonate), the
    claim forbids issuing further tokens."""
    other_admin = await make_agent(agency_id=admin.agency_id, roles=[system_roles["admin"]])
    third = await make_agent(agency_id=admin.agency_id)
    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)

    token = await _impersonate_agent(imp_client, agent_headers(admin), other_admin.id)
    chain_agent = await imp_client.post(
        f"/agencies/me/members/{third.id}/impersonate", headers=_bearer(token)
    )
    assert chain_agent.status_code == 403
    assert "impersonation" in chain_agent.json()["detail"].lower()
    chain_expat = await imp_client.post(
        f"/expat-users/{expat.id}/impersonate", headers=_bearer(token)
    )
    assert chain_expat.status_code == 403


async def test_refresh_with_impersonation_claim_rejected(
    imp_client: AsyncClient, db_session: AsyncSession, admin: Agent, make_agent: MakeAgent
) -> None:
    """Defensive depth: craft a refresh token carrying the claim AND a
    valid jti row — without the claim check it would rotate fine; the
    claim alone must kill it."""
    target = await make_agent(agency_id=admin.agency_id)
    settings = get_settings()
    jti = uuid.uuid4()
    now = datetime.now(UTC)
    db_session.add(
        RefreshToken(
            jti=jti,
            actor_type=Audience.AGENT.value,
            actor_id=target.id,
            expires_at=now + timedelta(days=7),
        )
    )
    await db_session.commit()
    forged = jose_jwt.encode(
        {
            "sub": str(target.id),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(days=7)).timestamp()),
            "type": "refresh",
            "audience": "agent",
            "jti": str(jti),
            "impersonator_id": str(admin.id),
        },
        settings.jwt_refresh_secret,
        algorithm=settings.jwt_algorithm,
    )
    response = await imp_client.post("/auth/agent/refresh", json={"refresh_token": forged})
    assert response.status_code == 401
    assert "impersonation" in response.json()["detail"].lower()


async def test_denied_surface_under_impersonation(
    imp_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Impersonating an ADMIN: the target's permissions would allow all
    of this — the claim alone forbids it (structure mutations + session
    lifecycle), with attribution poisoning as the criterion."""
    other_admin = await make_agent(agency_id=admin.agency_id, roles=[system_roles["admin"]])
    third = await make_agent(agency_id=admin.agency_id)
    token = await _impersonate_agent(imp_client, agent_headers(admin), other_admin.id)
    headers = _bearer(token)

    attempts = [
        ("PATCH", "/agencies/me", {"name": "Hijacked"}),
        (
            "POST",
            "/agencies/me/invitations",
            {"email": "x@example.com", "role_id": str(uuid.uuid4())},
        ),
        ("POST", "/agencies/me/roles", {"name": "x", "permission_ids": []}),
        ("PUT", f"/agencies/me/members/{third.id}/roles", {"role_ids": []}),
        ("POST", "/auth/agent/logout", {"refresh_token": "whatever"}),
    ]
    for method, url, payload in attempts:
        response = await imp_client.request(method, url, headers=headers, json=payload)
        assert response.status_code == 403, (method, url)
        assert "impersonation" in response.json()["detail"].lower()

    # Reads keep working — that is the point of the feature.
    assert (await imp_client.get("/agencies/me", headers=headers)).status_code == 200
    assert (await imp_client.get("/cases", headers=headers)).status_code == 200


async def test_expat_portal_read_only_under_impersonation(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """uploaded_by=EXPAT means 'the client provided this piece' in the
    validation flow — never forged under the mask. Reads stay open."""
    expat = await make_expat_user()
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    response = await imp_client.post(
        f"/expat-users/{expat.id}/impersonate", headers=agent_headers(admin)
    )
    token = response.json()["access_token"]
    headers = _bearer(token)

    assert (await imp_client.get("/expat/cases", headers=headers)).status_code == 200
    assert (
        await imp_client.get(f"/expat/cases/{case.id}/documents", headers=headers)
    ).status_code == 200
    upload = await imp_client.post(f"/expat/cases/{case.id}/documents", headers=headers)
    assert upload.status_code == 403
    delete_doc = await imp_client.delete(
        f"/expat/cases/{case.id}/documents/{uuid.uuid4()}", headers=headers
    )
    assert delete_doc.status_code == 403


async def test_no_elevation_target_permissions_apply(
    imp_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Step-18 invariant confirmed: an admin impersonating a
    case_manager works with the TARGET's permissions — role.manage is
    refused, the ceiling cannot be bypassed from below or above."""
    case_manager = await make_agent(agency_id=admin.agency_id, roles=[system_roles["case_manager"]])
    token = await _impersonate_agent(imp_client, agent_headers(admin), case_manager.id)
    response = await imp_client.get("/permissions", headers=_bearer(token))
    assert response.status_code == 403  # target lacks role.manage; admin's rights don't leak


# --- audit log & expiry --------------------------------------------------------------


async def test_impersonation_log_rows(
    imp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    target = await make_agent(agency_id=admin.agency_id)
    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)

    await _impersonate_agent(imp_client, agent_headers(admin), target.id)
    await imp_client.post(f"/expat-users/{expat.id}/impersonate", headers=agent_headers(admin))

    rows = (await db_session.execute(select(ImpersonationLog))).scalars().all()
    by_target = {row.target_id: row for row in rows}
    assert len(rows) == 2
    assert by_target[target.id].target_type == "agent"
    assert by_target[expat.id].target_type == "expat"
    for row in rows:
        assert row.impersonator_agent_id == admin.id
        # created_at is DB-clock (server_default now()), expires_at is
        # Python-clock — allow the inter-clock skew.
        remaining = row.expires_at - row.created_at
        assert timedelta(minutes=29) < remaining < timedelta(minutes=31)


async def test_expired_impersonation_token_rejected(
    imp_client: AsyncClient, admin: Agent, make_agent: MakeAgent
) -> None:
    target = await make_agent(agency_id=admin.agency_id)
    expired = create_access_token(
        str(target.id),
        Audience.AGENT,
        extra_claims={"impersonator_id": str(admin.id)},
        expires_minutes=-1,
    )
    response = await imp_client.get("/auth/agent/me", headers=_bearer(expired))
    assert response.status_code == 401


def test_impersonation_denylist_boot_check() -> None:
    """Real app: every denylist entry matches a declared route. A
    synthetic app missing them: refuse to boot."""
    from fastapi import FastAPI

    from src.core.rbac.integrity import (
        StartupError,
        assert_impersonation_denylist_declared,
    )
    from src.main import app

    assert_impersonation_denylist_declared(app)  # must not raise

    with pytest.raises(StartupError, match="IMPERSONATION_DENIED"):
        assert_impersonation_denylist_declared(FastAPI())
