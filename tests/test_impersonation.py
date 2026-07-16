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
    return await make_agent(role=system_roles["admin"], first_name="Alice", last_name="Admin")


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
        agency_id=admin.agency_id, role=system_roles["member"], first_name="Tom"
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
        actor = await make_agent(role=system_roles[role_name])
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
    other_admin = await make_agent(agency_id=admin.agency_id, role=system_roles["admin"])
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
    other_admin = await make_agent(agency_id=admin.agency_id, role=system_roles["admin"])
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
        ("PUT", f"/agencies/me/members/{third.id}/role", {"role_id": str(uuid.uuid4())}),
        ("POST", "/auth/agent/logout", {"refresh_token": "whatever"}),
    ]
    for method, url, payload in attempts:
        response = await imp_client.request(method, url, headers=headers, json=payload)
        assert response.status_code == 403, (method, url)
        assert "impersonation" in response.json()["detail"].lower()

    # Reads keep working — that is the point of the feature.
    assert (await imp_client.get("/agencies/me", headers=headers)).status_code == 200
    assert (await imp_client.get("/cases", headers=headers)).status_code == 200


# Every EXPAT write family, with placeholder ids — the read-only gate
# fires in enforce(), BEFORE ownership/validation, so bogus ids still
# prove the block. One entry per binding (documents, requirements, case
# requirements, step validation, comments). There is no expat profile
# write endpoint (nothing to cover there).
_U = "00000000-0000-0000-0000-000000000001"
EXPAT_WRITE_ATTEMPTS: list[tuple[str, str]] = [
    ("POST", f"/expat/cases/{_U}/documents"),
    ("DELETE", f"/expat/cases/{_U}/documents/{_U}"),
    ("PUT", f"/expat/cases/{_U}/requirements/{_U}"),
    ("POST", f"/expat/cases/{_U}/requirements/{_U}/document"),
    ("PUT", f"/expat/cases/{_U}/case-requirements/{_U}"),
    ("POST", f"/expat/cases/{_U}/steps/{_U}/validate"),
    ("POST", f"/expat/cases/{_U}/steps/{_U}/comments"),
    ("PATCH", f"/expat/cases/{_U}/steps/{_U}/comments/{_U}"),
    ("DELETE", f"/expat/cases/{_U}/steps/{_U}/comments/{_U}"),
]


async def _impersonate_expat_token(
    client: AsyncClient, admin_headers: dict[str, str], expat_id: uuid.UUID
) -> str:
    response = await client.post(f"/expat-users/{expat_id}/impersonate", headers=admin_headers)
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def test_expat_portal_read_only_under_impersonation(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """(a) + (c): EVERY expat write family is 403 impersonation.read_only
    under the mask (method rule, not a route list); reads stay open —
    that is the point of the mode."""
    expat = await make_expat_user()
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    headers = _bearer(await _impersonate_expat_token(imp_client, agent_headers(admin), expat.id))

    for method, url in EXPAT_WRITE_ATTEMPTS:
        response = await imp_client.request(method, url, headers=headers, json={})
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["code"] == "impersonation.read_only", (method, url)

    # (c) reads pass: the mode exists to SEE what the client sees.
    assert (await imp_client.get("/expat/cases", headers=headers)).status_code == 200
    assert (await imp_client.get(f"/expat/cases/{case.id}", headers=headers)).status_code == 200
    assert (
        await imp_client.get(f"/expat/cases/{case.id}/documents", headers=headers)
    ).status_code == 200
    assert (await imp_client.get("/auth/expat/me", headers=headers)).status_code == 200


async def test_expat_writes_pass_with_real_token(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """(b) non-regression: the SAME requests with a real expat token are
    never stopped by the impersonation gate (they fail later on
    ownership/validation, or succeed), and a genuine fulfillment write
    still lands — while the same write under the mask is refused and
    changes nothing."""
    expat = await make_expat_user()
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    real_headers = _bearer(create_access_token(str(expat.id), Audience.EXPAT))

    for method, url in EXPAT_WRITE_ATTEMPTS:
        response = await imp_client.request(method, url, headers=real_headers, json={})
        assert response.status_code != 403, (method, url, response.text)

    # Full happy path: a materialized requirement, fulfilled by the REAL
    # client (200) — then the impersonator tries to overwrite it (403,
    # value untouched).
    h = agent_headers(admin)
    tid = (await imp_client.post("/journeys", headers=h, json={"name": "T"})).json()["id"]
    sid = (await imp_client.post(f"/journeys/{tid}/steps", headers=h, json={"name": "S"})).json()[
        "id"
    ]
    await imp_client.post(
        f"/journeys/{tid}/fields",
        headers=h,
        json={"kind": "base_field", "reference": "passport_number"},
    )
    await imp_client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=h,
        json={"kind": "base_field", "reference": "passport_number", "scope": "principal"},
    )
    steps = (
        await imp_client.post(
            f"/cases/{case.id}/journey", headers=h, json={"journey_template_id": tid}
        )
    ).json()
    started = await imp_client.patch(
        f"/cases/{case.id}/steps/{steps[0]['id']}", headers=h, json={"status": "in_progress"}
    )
    requirement_id = started.json()["requirements"][0]["id"]

    fulfilled = await imp_client.put(
        f"/expat/cases/{case.id}/requirements/{requirement_id}",
        headers=real_headers,
        json={"value": "AB12345"},
    )
    assert fulfilled.status_code == 200, fulfilled.text

    mask = _bearer(await _impersonate_expat_token(imp_client, agent_headers(admin), expat.id))
    forged = await imp_client.put(
        f"/expat/cases/{case.id}/requirements/{requirement_id}",
        headers=mask,
        json={"value": "FORGED"},
    )
    assert forged.status_code == 403
    assert forged.json()["code"] == "impersonation.read_only"
    detail = (await imp_client.get(f"/expat/cases/{case.id}", headers=mask)).json()
    values = [req["value"] for step in detail["timeline"] for req in step["requirements"]]
    assert values == ["AB12345"]  # nothing was written under the mask


async def test_expat_logout_allowed_under_impersonation(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """(d) the write allowlist: logout passes the impersonation gate (the
    client-space session flow must terminate cleanly). It then fails on
    the refresh token itself (401: the mask holds none) — proving the
    403 read-only wall is NOT what answered."""
    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    headers = _bearer(await _impersonate_expat_token(imp_client, agent_headers(admin), expat.id))

    response = await imp_client.post(
        "/auth/expat/logout", headers=headers, json={"refresh_token": "not-a-refresh-token"}
    )
    assert response.status_code == 401  # past the gate; no refresh to revoke
    assert response.json()["code"] != "impersonation.read_only"


async def test_future_expat_write_endpoint_locked_by_default(
    imp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """(e) structural guarantee: a BRAND NEW expat write endpoint —
    registered at test time, unknown to any list — is born locked under
    impersonation (method rule) while staying reachable with a real
    expat token. No developer action required."""
    from shared.models.rbac import ProtectedResource
    from src.main import app

    path = "/expat/cases/{case_id}/fictive-write"

    async def fictive_write(case_id: uuid.UUID) -> dict[str, bool]:
        return {"ok": True}

    db_session.add(ProtectedResource(method="POST", route=path, audience="expat"))
    await db_session.commit()
    app.post(path)(fictive_write)
    try:
        expat = await make_expat_user()
        await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
        mask = _bearer(await _impersonate_expat_token(imp_client, agent_headers(admin), expat.id))
        blocked = await imp_client.post(f"/expat/cases/{uuid.uuid4()}/fictive-write", headers=mask)
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "impersonation.read_only"

        real = _bearer(create_access_token(str(expat.id), Audience.EXPAT))
        allowed = await imp_client.post(f"/expat/cases/{uuid.uuid4()}/fictive-write", headers=real)
        assert allowed.status_code == 200
        assert allowed.json() == {"ok": True}
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) != path]


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
    case_manager = await make_agent(agency_id=admin.agency_id, role=system_roles["case_manager"])
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

    with pytest.raises(StartupError, match="Enforcement route entries"):
        assert_impersonation_denylist_declared(FastAPI())


# --- pending-invitation principal: impersonation reads, login stays shut -----------
# The activated_at gate protects LOGIN, not impersonation. An agent may "see as"
# a client who has NOT accepted the invitation (activated_at NULL); the dossier
# space exists. The exemption is keyed on impersonator_id — a SIGNED claim
# (decode_access_token verifies the HMAC), unforgeable without the expat secret.


async def test_impersonate_non_activated_principal_opens_the_dossier(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Eric's bug dies here: the principal's invitation is still pending
    (activated_at NULL); 'see as client' opens the dossier timeline."""
    pending = await make_expat_user(activated=False)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=pending.id)
    token = await _impersonate_expat_token(imp_client, agent_headers(admin), pending.id)
    listing = await imp_client.get("/expat/cases", headers=_bearer(token))
    assert listing.status_code == 200, listing.text
    assert any(c["id"] == str(case.id) for c in listing.json())
    detail = await imp_client.get(f"/expat/cases/{case.id}", headers=_bearer(token))
    assert detail.status_code == 200, detail.text


async def test_non_activated_expat_normal_login_stays_401(
    imp_client: AsyncClient,
    make_expat_user: MakeExpatUser,
) -> None:
    """Non-regression — the gate's original purpose. A non-activated expat
    cannot log in normally (no impersonator_id anywhere): 401. Anti-enum: the
    message is the generic 'Invalid credentials.', not 'Account not activated'
    (login is PUBLIC and never reaches the token gate)."""
    pending = await make_expat_user(activated=False)
    resp = await imp_client.post(
        "/auth/expat/login", json={"email": pending.email, "password": "not-a-real-password"}
    )
    assert resp.status_code == 401, resp.text


async def test_non_activated_expat_token_without_impersonator_id_is_401(
    imp_client: AsyncClient,
    make_expat_user: MakeExpatUser,
) -> None:
    """A validly SIGNED expat token WITHOUT impersonator_id, for a
    non-activated account, is still rejected at the token gate — the exemption
    is strictly keyed on the impersonation claim, not on non-activation."""
    pending = await make_expat_user(activated=False)
    forged = create_access_token(str(pending.id), Audience.EXPAT)  # no extra_claims
    resp = await imp_client.get("/expat/cases", headers=_bearer(forged))
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Account not activated."


async def test_impersonate_activated_principal_unchanged(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """An ACTIVATED principal's impersonation is unchanged — the fix only
    relaxes the NULL-activated branch."""
    active = await make_expat_user(activated=True)
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=active.id)
    token = await _impersonate_expat_token(imp_client, agent_headers(admin), active.id)
    assert (await imp_client.get("/expat/cases", headers=_bearer(token))).status_code == 200


async def test_impersonation_write_mask_holds_activated_and_not(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The read-only mask holds under impersonation for BOTH a non-activated
    and an activated principal — the fix opens reads, never writes."""
    write_method, write_url = "PUT", f"/expat/cases/{_U}/requirements/{_U}"
    for activated in (False, True):
        expat = await make_expat_user(activated=activated)
        await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
        token = await _impersonate_expat_token(imp_client, agent_headers(admin), expat.id)
        resp = await imp_client.request(write_method, write_url, headers=_bearer(token), json={})
        assert resp.status_code == 403, (activated, resp.text)
        assert resp.json()["code"] == "impersonation.read_only", activated


async def test_cross_agency_impersonation_denied_activated_or_not(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Agency scoping is unchanged: an agent cannot impersonate a principal of
    ANOTHER agency, activated or not — 404 at the mint
    (expat_is_impersonable_in_agency: principal OR member, always agency-scoped)."""
    for activated in (False, True):
        foreign = await make_expat_user(activated=activated)
        await make_client_case(principal_expat_user_id=foreign.id)  # a DIFFERENT agency
        resp = await imp_client.post(
            f"/expat-users/{foreign.id}/impersonate", headers=agent_headers(admin)
        )
        assert resp.status_code == 404, (activated, resp.text)


# --- "voir comme" PAR PERSONNE : le membre est impersonable ----------------------------


async def test_member_impersonation_lands_on_their_filtered_projection(
    imp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """A dossier MEMBER (case_person.expat_user_id) is now a valid 'see as'
    target — and the minted view is THEIR OWN filtered projection (their
    requirements only), not the principal's."""
    from tests.test_case_members import _all_requirements, _setup_with_db

    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal-imp@x.io")
    member = await make_expat_user(email="member-imp@x.io")
    case_id, _ = await _setup_with_db(
        imp_client, db_session, admin, headers, make_client_case, principal, member.email
    )

    minted = await imp_client.post(f"/expat-users/{member.id}/impersonate", headers=headers)
    assert minted.status_code == 200, minted.text
    mask = _bearer(minted.json()["access_token"])

    detail = await imp_client.get(f"/expat/cases/{case_id}", headers=mask)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["viewer_role"] == "member"
    reqs = _all_requirements(body)
    # The member's projection exactly: their own date_of_birth, nothing of
    # the principal (no passport, no leaked value).
    assert {r["reference"] for r in reqs} == {"date_of_birth"}
    assert all(r["person_label"] == "Marie Dupont" for r in reqs)


async def test_mask_blocks_member_fulfill_under_impersonation(
    imp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The legal read-only mask WINS over the member's contributor rights:
    seeing as a member never allows filling in their name — filling for
    someone is an AGENT-face gesture."""
    from tests.test_case_members import _all_requirements, _setup_with_db

    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal-imp2@x.io")
    member = await make_expat_user(email="member-imp2@x.io")
    case_id, _ = await _setup_with_db(
        imp_client, db_session, admin, headers, make_client_case, principal, member.email
    )
    minted = await imp_client.post(f"/expat-users/{member.id}/impersonate", headers=headers)
    mask = _bearer(minted.json()["access_token"])
    [req] = _all_requirements(
        (await imp_client.get(f"/expat/cases/{case_id}", headers=mask)).json()
    )

    denied = await imp_client.put(
        f"/expat/cases/{case_id}/requirements/{req['id']}",
        headers=mask,
        json={"value": "1990-02-01"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "impersonation.read_only"


async def test_foreign_agency_member_is_not_impersonable(
    imp_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The widened predicate stays agency-scoped: a MEMBER of another
    agency's case is 404 at the mint, exactly like a foreign principal."""
    from tests.test_case_members import _setup_with_db

    other_admin = await make_agent(role=system_roles["admin"], email="other-imp@x.io")
    # a member in the OTHER agency's case
    foreign_principal = await make_expat_user(email="fp-imp@x.io")
    foreign_member = await make_expat_user(email="fm-imp@x.io")
    await _setup_with_db(
        imp_client,
        db_session,
        other_admin,
        agent_headers(other_admin),
        make_client_case,
        foreign_principal,
        foreign_member.email,
    )
    resp = await imp_client.post(
        f"/expat-users/{foreign_member.id}/impersonate", headers=agent_headers(admin)
    )
    assert resp.status_code == 404


async def test_person_without_account_has_nothing_to_target(
    imp_client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """No email → no expat_user → nothing to impersonate. The contract the
    front greys on: PersonResponse.expat_user_id is null for such a person."""
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal-imp3@x.io")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    created = await imp_client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Sans Compte", "relationship": "child"},  # NO email
    )
    assert created.status_code == 201, created.text
    assert created.json()["expat_user_id"] is None  # the greying anchor
    detail = (await imp_client.get(f"/cases/{case.id}", headers=headers)).json()
    person = next(p for p in detail["persons"] if p["full_name"] == "Sans Compte")
    assert person["expat_user_id"] is None and person["email"] is None
