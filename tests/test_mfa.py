"""2FA TOTP battery (bloc 2): enrollment, two-step login, brute-force
cap, one-time backup codes, double-factor disable, mfa_token quarantine,
impersonation interplay, secret hygiene.

The mfa_token is an ephemeral JWT typed "mfa_pending": every
access-authenticated route rejects it structurally (the decoders check
the type claim); its jti keys the server-side attempts counter."""

import pyotp
import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core.enums import Audience
from src.core.security import create_access_token
from tests.plugins.agent_plugin import DEFAULT_PASSWORD, AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _expat_headers(expat: ExpatUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(expat.id), Audience.EXPAT)}"}


async def _enroll_agent(client: AsyncClient, headers: dict[str, str]) -> tuple[str, list[str]]:
    """setup + enable → (secret, backup_codes)."""
    setup = await client.post("/auth/agent/2fa/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert "Nidria" in setup.json()["otpauth_uri"]
    enable = await client.post(
        "/auth/agent/2fa/enable",
        headers=headers,
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert enable.status_code == 200, enable.text
    codes = enable.json()["backup_codes"]
    assert len(codes) == 8 and len(set(codes)) == 8
    return secret, codes


async def _login_step1(client: AsyncClient, email: str) -> str:
    """Password OK + 2FA enabled → the ephemeral challenge, no tokens."""
    response = await client.post(
        "/auth/agent/login", json={"email": email, "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("mfa_required") is True
    assert "access_token" not in body and "refresh_token" not in body
    return str(body["mfa_token"])


# --- (a) full happy path ---------------------------------------------------------------


async def test_setup_enable_then_two_step_login(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agent = await make_agent(role=system_roles["admin"], email="totp@example.com")
    headers = agent_headers(agent)
    secret, _codes = await _enroll_agent(client, headers)

    mfa_token = await _login_step1(client, agent.email)
    verified = await client.post(
        "/auth/agent/2fa/verify",
        json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()},
    )
    assert verified.status_code == 200, verified.text
    pair = verified.json()
    me = await client.get(
        "/auth/agent/me", headers={"Authorization": f"Bearer {pair['access_token']}"}
    )
    assert me.status_code == 200


# --- (b) brute force: the challenge dies at the cap --------------------------------------


async def test_five_bad_codes_kill_the_challenge(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agent = await make_agent(role=system_roles["admin"], email="brute@example.com")
    secret, _ = await _enroll_agent(client, agent_headers(agent))
    mfa_token = await _login_step1(client, agent.email)

    for _ in range(4):
        response = await client.post(
            "/auth/agent/2fa/verify", json={"mfa_token": mfa_token, "code": "000000"}
        )
        assert response.status_code == 422
        assert response.json()["code"] == "auth.mfa_invalid_code"
    fifth = await client.post(
        "/auth/agent/2fa/verify", json={"mfa_token": mfa_token, "code": "000000"}
    )
    assert fifth.status_code == 401
    assert fifth.json()["code"] == "auth.mfa_too_many_attempts"

    # The challenge is dead: even the RIGHT code is refused now.
    right = await client.post(
        "/auth/agent/2fa/verify",
        json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()},
    )
    assert right.status_code == 401
    assert right.json()["code"] == "auth.mfa_token_expired"


# --- (c) backup codes are one-time --------------------------------------------------------


async def test_backup_code_consumed_once(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agent = await make_agent(role=system_roles["admin"], email="backup@example.com")
    _, codes = await _enroll_agent(client, agent_headers(agent))

    token1 = await _login_step1(client, agent.email)
    first = await client.post(
        "/auth/agent/2fa/verify", json={"mfa_token": token1, "code": codes[0]}
    )
    assert first.status_code == 200, first.text

    token2 = await _login_step1(client, agent.email)
    replay = await client.post(
        "/auth/agent/2fa/verify", json={"mfa_token": token2, "code": codes[0]}
    )
    assert replay.status_code == 422  # consumed — never twice
    another = await client.post(
        "/auth/agent/2fa/verify", json={"mfa_token": token2, "code": codes[1]}
    )
    assert another.status_code == 200


# --- (d) the mfa_token opens NOTHING else --------------------------------------------------


async def test_mfa_token_rejected_everywhere_else(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agent = await make_agent(role=system_roles["admin"], email="scope@example.com")
    await _enroll_agent(client, agent_headers(agent))
    mfa_token = await _login_step1(client, agent.email)

    sneaky = {"Authorization": f"Bearer {mfa_token}"}
    assert (await client.get("/auth/agent/me", headers=sneaky)).status_code == 401
    assert (await client.get("/cases", headers=sneaky)).status_code == 401


# --- (e) disable demands BOTH factors ------------------------------------------------------


async def test_disable_requires_password_and_code(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agent = await make_agent(role=system_roles["admin"], email="disable@example.com")
    headers = agent_headers(agent)
    secret, _ = await _enroll_agent(client, headers)

    wrong_password = await client.post(
        "/auth/agent/2fa/disable",
        headers=headers,
        json={"current_password": "not-it", "code": pyotp.TOTP(secret).now()},
    )
    assert wrong_password.status_code == 403
    assert wrong_password.json()["code"] == "auth.wrong_password"

    wrong_code = await client.post(
        "/auth/agent/2fa/disable",
        headers=headers,
        json={"current_password": DEFAULT_PASSWORD, "code": "000000"},
    )
    assert wrong_code.status_code == 422
    assert wrong_code.json()["code"] == "auth.mfa_invalid_code"

    ok = await client.post(
        "/auth/agent/2fa/disable",
        headers=headers,
        json={"current_password": DEFAULT_PASSWORD, "code": pyotp.TOTP(secret).now()},
    )
    assert ok.status_code == 200
    # Back to a plain one-step login.
    login = await client.post(
        "/auth/agent/login", json={"email": agent.email, "password": DEFAULT_PASSWORD}
    )
    assert "access_token" in login.json()


# --- (f) non-regression: login without 2FA is byte-identical -------------------------------


async def test_login_without_mfa_unchanged(
    client: AsyncClient, make_agent: MakeAgent, system_roles: dict[str, Role]
) -> None:
    agent = await make_agent(role=system_roles["admin"], email="plain@example.com")
    login = await client.post(
        "/auth/agent/login", json={"email": agent.email, "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 200
    body = login.json()
    assert set(body) == {"access_token", "refresh_token", "token_type"}
    assert "mfa_token" not in body


# --- (g) impersonation never crosses the target's 2FA -------------------------------------


async def test_impersonation_bypasses_target_mfa_and_blocks_2fa_endpoints(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    # A client with ACTIVE 2FA.
    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    eh = _expat_headers(expat)
    setup = await client.post("/auth/expat/2fa/setup", headers=eh)
    secret = setup.json()["secret"]
    enabled = await client.post(
        "/auth/expat/2fa/enable", headers=eh, json={"code": pyotp.TOTP(secret).now()}
    )
    assert enabled.status_code == 200

    # Impersonation issuance ignores the factor (the agent does not own it)...
    issued = await client.post(f"/expat-users/{expat.id}/impersonate", headers=headers)
    assert issued.status_code == 200
    mask = {"Authorization": f"Bearer {issued.json()['access_token']}"}
    assert (await client.get("/expat/cases", headers=mask)).status_code == 200

    # ...and no 2FA endpoint is reachable under the mask (expat: read-only
    # wall; agent: session-lifecycle denylist).
    blocked = await client.post("/auth/expat/2fa/setup", headers=mask)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "impersonation.read_only"

    colleague = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    issued = await client.post(f"/agencies/me/members/{colleague.id}/impersonate", headers=headers)
    agent_mask = {"Authorization": f"Bearer {issued.json()['access_token']}"}
    for url in ("/auth/agent/2fa/setup", "/auth/agent/2fa/enable", "/auth/agent/2fa/disable"):
        denied = await client.post(url, headers=agent_mask, json={"code": "000000"})
        assert denied.status_code == 403, url
        assert denied.json()["code"] == "impersonation.denied"


# --- (h) the secret never leaks after setup -------------------------------------------------


async def test_secret_never_exposed_after_setup(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agent = await make_agent(role=system_roles["admin"], email="hygiene@example.com")
    headers = agent_headers(agent)
    secret, codes = await _enroll_agent(client, headers)

    enable_like_responses = [
        await client.get("/auth/agent/2fa", headers=headers),
        await client.get("/auth/agent/me", headers=headers),
    ]
    mfa_token = await _login_step1(client, agent.email)
    verified = await client.post(
        "/auth/agent/2fa/verify",
        json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()},
    )
    enable_like_responses.append(verified)
    for response in enable_like_responses:
        assert secret not in response.text
    status = await client.get("/auth/agent/2fa", headers=headers)
    assert status.json() == {"enabled": True, "backup_codes_left": 8}
