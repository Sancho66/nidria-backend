"""Double-auth battery: logins (generic 401), refresh rotation + reuse
detection, logout, expat activation (incl. 2nd-invitation guard),
forgot/reset password, cross-audience seals — all over HTTP against the
real app with the real RBAC baseline seeded."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.auth_tokens import PasswordResetToken, RefreshToken
from shared.models.expat_user import ExpatUser
from shared.models.invitation import CaseInvitation
from shared.models.rbac import Role
from src.core import email
from src.core.config import get_settings
from src.core.enums import InvitationStatus
from src.core.rbac.integrity import assert_all_routes_bound
from src.main import app
from tests.plugins.agent_plugin import DEFAULT_PASSWORD, AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCaseInvitation, MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def auth_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    """Real-app client with the RBAC baseline (incl. auth BINDINGS) seeded."""
    return client


# --- Agent login ----------------------------------------------------------------


async def test_agent_login_ok(auth_client: AsyncClient, make_agent: MakeAgent) -> None:
    agent = await make_agent(email="login@example.com", password="s3cret-pass")
    response = await auth_client.post(
        "/auth/agent/login", json={"email": "login@example.com", "password": "s3cret-pass"}
    )
    assert response.status_code == 200
    tokens = response.json()
    me = await auth_client.get(
        "/auth/agent/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["id"] == str(agent.id)


async def test_agent_login_failures_are_generic(
    auth_client: AsyncClient, make_agent: MakeAgent
) -> None:
    await make_agent(email="known@example.com", password="right-password")
    wrong_password = await auth_client.post(
        "/auth/agent/login", json={"email": "known@example.com", "password": "wrong-password"}
    )
    unknown_email = await auth_client.post(
        "/auth/agent/login", json={"email": "ghost@example.com", "password": "whatever123"}
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


# --- /me ---------------------------------------------------------------------------


async def test_agent_me_payload(
    auth_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agent = await make_agent(roles=[system_roles["member"]])
    response = await auth_client.get("/auth/agent/me", headers=agent_headers(agent))
    assert response.status_code == 200
    body = response.json()
    assert body["agency_id"] == str(agent.agency_id)
    assert body["roles"] == ["member"]
    assert "reminder.approve" in body["effective_permissions"]
    assert "note.view_confidential" not in body["effective_permissions"]


async def test_agent_me_requires_token(auth_client: AsyncClient) -> None:
    assert (await auth_client.get("/auth/agent/me")).status_code == 401


async def test_cross_audience_me_is_sealed(
    auth_client: AsyncClient,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    agent = await make_agent()
    expat = await make_expat_user()
    assert (
        await auth_client.get("/auth/expat/me", headers=agent_headers(agent))
    ).status_code == 401
    assert (
        await auth_client.get("/auth/agent/me", headers=expat_headers(expat))
    ).status_code == 401
    assert (
        await auth_client.get("/auth/expat/me", headers=expat_headers(expat))
    ).status_code == 200


# --- Refresh rotation -----------------------------------------------------------------


async def _login(auth_client: AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await auth_client.post(
        "/auth/agent/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return dict(response.json())


async def test_refresh_rotation_old_token_dies_and_reuse_revokes_family(
    auth_client: AsyncClient, make_agent: MakeAgent
) -> None:
    await make_agent(email="rotate@example.com", password=DEFAULT_PASSWORD)
    pair1 = await _login(auth_client, "rotate@example.com", DEFAULT_PASSWORD)

    # Rotation: refresh yields a new working pair.
    r2 = await auth_client.post(
        "/auth/agent/refresh", json={"refresh_token": pair1["refresh_token"]}
    )
    assert r2.status_code == 200
    pair2 = r2.json()
    assert pair2["refresh_token"] != pair1["refresh_token"]

    # Reusing the consumed refresh → 401 AND the whole family is revoked.
    reuse = await auth_client.post(
        "/auth/agent/refresh", json={"refresh_token": pair1["refresh_token"]}
    )
    assert reuse.status_code == 401
    after_reuse = await auth_client.post(
        "/auth/agent/refresh", json={"refresh_token": pair2["refresh_token"]}
    )
    assert after_reuse.status_code == 401


async def test_refresh_audience_mismatch(
    auth_client: AsyncClient,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
) -> None:
    await make_agent(email="agent-aud@example.com", password=DEFAULT_PASSWORD)
    agent_pair = await _login(auth_client, "agent-aud@example.com", DEFAULT_PASSWORD)
    # Agent refresh token on the EXPAT refresh endpoint → 401 (audience claim).
    response = await auth_client.post(
        "/auth/expat/refresh", json={"refresh_token": agent_pair["refresh_token"]}
    )
    assert response.status_code == 401

    expat = await make_expat_user(email="expat-aud@example.com", password=DEFAULT_PASSWORD)
    login = await auth_client.post(
        "/auth/expat/login",
        json={"email": expat.email, "password": DEFAULT_PASSWORD},
    )
    response = await auth_client.post(
        "/auth/agent/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert response.status_code == 401


async def test_refresh_with_garbage_token(auth_client: AsyncClient) -> None:
    response = await auth_client.post("/auth/agent/refresh", json={"refresh_token": "not-a-jwt"})
    assert response.status_code == 401


# --- Logout -----------------------------------------------------------------------------


async def test_logout_revokes_refresh_but_access_survives(
    auth_client: AsyncClient, make_agent: MakeAgent
) -> None:
    await make_agent(email="logout@example.com", password=DEFAULT_PASSWORD)
    pair = await _login(auth_client, "logout@example.com", DEFAULT_PASSWORD)
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    response = await auth_client.post(
        "/auth/agent/logout", json={"refresh_token": pair["refresh_token"]}, headers=headers
    )
    assert response.status_code == 200

    # The refresh token is dead…
    refresh = await auth_client.post(
        "/auth/agent/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refresh.status_code == 401
    # …but the access token stays valid until its expiry (documented).
    assert (await auth_client.get("/auth/agent/me", headers=headers)).status_code == 200


async def test_logout_rejects_foreign_refresh_token(
    auth_client: AsyncClient, make_agent: MakeAgent
) -> None:
    await make_agent(email="victim@example.com", password=DEFAULT_PASSWORD)
    await make_agent(email="attacker@example.com", password=DEFAULT_PASSWORD)
    victim_pair = await _login(auth_client, "victim@example.com", DEFAULT_PASSWORD)
    attacker_pair = await _login(auth_client, "attacker@example.com", DEFAULT_PASSWORD)

    response = await auth_client.post(
        "/auth/agent/logout",
        json={"refresh_token": victim_pair["refresh_token"]},
        headers={"Authorization": f"Bearer {attacker_pair['access_token']}"},
    )
    assert response.status_code == 401
    # Victim's refresh still alive.
    refresh = await auth_client.post(
        "/auth/agent/refresh", json={"refresh_token": victim_pair["refresh_token"]}
    )
    assert refresh.status_code == 200


# --- Expat activation ----------------------------------------------------------------------


async def test_expat_activation_flow(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    expat = await make_expat_user(activated=False, email="newcomer@example.com")
    case = await make_client_case(principal_expat_user_id=expat.id)
    invitation = await make_case_invitation(case=case, email="newcomer@example.com")

    response = await auth_client.post(
        "/auth/expat/activate",
        json={"token": invitation.token, "password": "fresh-password-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["already_active"] is False
    assert body["access_token"]

    await db_session.refresh(expat)
    await db_session.refresh(invitation)
    assert expat.activated_at is not None
    assert invitation.status == InvitationStatus.ACCEPTED

    login = await auth_client.post(
        "/auth/expat/login",
        json={"email": "newcomer@example.com", "password": "fresh-password-1"},
    )
    assert login.status_code == 200


async def test_activate_invalid_and_expired_tokens(
    auth_client: AsyncClient,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    assert (
        await auth_client.post(
            "/auth/expat/activate", json={"token": "unknown", "password": "password123"}
        )
    ).status_code == 400

    case = await make_client_case()
    expired = await make_case_invitation(
        case=case, expires_at=datetime.now(UTC) - timedelta(days=1)
    )
    assert (
        await auth_client.post(
            "/auth/expat/activate", json={"token": expired.token, "password": "password123"}
        )
    ).status_code == 400


async def test_activate_already_active_never_touches_password(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    make_case_invitation: MakeCaseInvitation,
) -> None:
    expat = await make_expat_user(email="active@example.com", password="original-pass-1")
    case = await make_client_case(principal_expat_user_id=expat.id)
    invitation = await make_case_invitation(case=case, email="active@example.com")

    response = await auth_client.post(
        "/auth/expat/activate",
        json={"token": invitation.token, "password": "attacker-pass-1"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "already_active": True,
        "access_token": None,
        "refresh_token": None,
        "token_type": "bearer",
    }
    invitation_row = await db_session.get(CaseInvitation, invitation.id)
    assert invitation_row is not None and invitation_row.status == InvitationStatus.ACCEPTED

    # Original password still works; the invited-password does not.
    ok = await auth_client.post(
        "/auth/expat/login",
        json={"email": "active@example.com", "password": "original-pass-1"},
    )
    assert ok.status_code == 200
    ko = await auth_client.post(
        "/auth/expat/login",
        json={"email": "active@example.com", "password": "attacker-pass-1"},
    )
    assert ko.status_code == 401


async def test_expat_login_before_activation_401(
    auth_client: AsyncClient, make_expat_user: MakeExpatUser
) -> None:
    await make_expat_user(activated=False, email="pending@example.com")
    response = await auth_client.post(
        "/auth/expat/login",
        json={"email": "pending@example.com", "password": DEFAULT_PASSWORD},
    )
    assert response.status_code == 401


# --- Forgot / reset password -------------------------------------------------------------------


async def test_forgot_password_does_not_reveal_accounts(
    auth_client: AsyncClient, make_agent: MakeAgent
) -> None:
    await make_agent(email="real@example.com")
    existing = await auth_client.post(
        "/auth/agent/forgot-password", json={"email": "real@example.com"}
    )
    unknown = await auth_client.post(
        "/auth/agent/forgot-password", json={"email": "nobody@example.com"}
    )
    assert existing.status_code == unknown.status_code == 200
    assert existing.json() == unknown.json()
    # Mail only for the real account — with the frontend reset link in
    # BOTH multipart parts (text fallback and HTML).
    assert len(email.outbox) == 1
    sent = email.outbox[0]
    assert sent.to == "real@example.com"
    link_prefix = f"{get_settings().frontend_url}/reset-password/"
    assert link_prefix in sent.body
    assert sent.html is not None and link_prefix in sent.html


async def test_forgot_password_activated_expat_gets_space_link(
    auth_client: AsyncClient, make_expat_user: MakeExpatUser
) -> None:
    await make_expat_user(activated=True, email="active@example.com")
    response = await auth_client.post(
        "/auth/expat/forgot-password", json={"email": "active@example.com"}
    )
    assert response.status_code == 200
    assert len(email.outbox) == 1
    sent = email.outbox[0]
    # The expat space lives under /space on the frontend route map.
    link_prefix = f"{get_settings().frontend_url}/space/reset-password/"
    assert link_prefix in sent.body
    assert sent.html is not None and link_prefix in sent.html


async def test_forgot_password_non_activated_expat_is_silent(
    auth_client: AsyncClient, make_expat_user: MakeExpatUser
) -> None:
    await make_expat_user(activated=False, email="not-yet@example.com")
    response = await auth_client.post(
        "/auth/expat/forgot-password", json={"email": "not-yet@example.com"}
    )
    assert response.status_code == 200
    assert email.outbox == []


async def test_reset_password_full_flow(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
) -> None:
    await make_agent(email="reset@example.com", password="old-password-1")
    pair = await _login(auth_client, "reset@example.com", "old-password-1")

    await auth_client.post("/auth/agent/forgot-password", json={"email": "reset@example.com"})
    token_row = (await db_session.execute(select(PasswordResetToken))).scalar_one()

    response = await auth_client.post(
        "/auth/agent/reset-password",
        json={"token": token_row.token, "password": "new-password-1"},
    )
    assert response.status_code == 200

    # Old password dead, new one works.
    assert (
        await auth_client.post(
            "/auth/agent/login",
            json={"email": "reset@example.com", "password": "old-password-1"},
        )
    ).status_code == 401
    assert (
        await auth_client.post(
            "/auth/agent/login",
            json={"email": "reset@example.com", "password": "new-password-1"},
        )
    ).status_code == 200

    # All pre-reset refresh tokens are revoked.
    assert (
        await auth_client.post("/auth/agent/refresh", json={"refresh_token": pair["refresh_token"]})
    ).status_code == 401

    # Token is single-use.
    assert (
        await auth_client.post(
            "/auth/agent/reset-password",
            json={"token": token_row.token, "password": "another-pass-1"},
        )
    ).status_code == 400


async def test_reset_password_expired_token_400(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
) -> None:
    agent = await make_agent(email="late@example.com")
    db_session.add(
        PasswordResetToken(
            actor_type="agent",
            actor_id=agent.id,
            token="expired-token",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await db_session.commit()
    response = await auth_client.post(
        "/auth/agent/reset-password",
        json={"token": "expired-token", "password": "new-password-1"},
    )
    assert response.status_code == 400


# --- Integration guard ---------------------------------------------------------------------------


async def test_real_app_routes_all_bound(db_session: AsyncSession, rbac_baseline: None) -> None:
    """The boot check passes on the REAL app with the seeded baseline —
    a route added without a BINDINGS entry fails here first."""
    await assert_all_routes_bound(app, db_session)


async def test_refresh_tokens_are_persisted_per_actor(
    auth_client: AsyncClient, db_session: AsyncSession, make_expat_user: MakeExpatUser
) -> None:
    expat = await make_expat_user(email="rows@example.com", password=DEFAULT_PASSWORD)
    await auth_client.post(
        "/auth/expat/login", json={"email": "rows@example.com", "password": DEFAULT_PASSWORD}
    )
    rows = (await db_session.execute(select(RefreshToken))).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor_type == "expat"
    assert rows[0].actor_id == expat.id
    assert rows[0].revoked_at is None


async def test_non_activated_expat_row_has_no_password(
    make_expat_user: MakeExpatUser, db_session: AsyncSession
) -> None:
    expat = await make_expat_user(activated=False)
    row = await db_session.get(ExpatUser, expat.id)
    assert row is not None
    assert row.password_hash is None and row.activated_at is None
