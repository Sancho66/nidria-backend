"""Error i18n, wave E1 — ACCOUNTS & ACCESS, the refusals an AGENT reads.

Each refusal keeps its English `detail` byte-identical (fallback, logs) and
gains a stable dotted `code` + `params` the front translates. Families:

- authentication: ONE code for every login failure (non-revealing), ONE
  for every rejected session token (access, refresh, unknown or offboarded
  actor, missing), the login step-2 token sending back to step 1;
- the permission matrix: one code, the missing key in params;
- members: not found, the anti-lockout (the offboarding dialog branches on
  it — it used to answer the category `conflict`, which the dialog's
  « no code » test never matched), the self seat flip;
- invitations: role, email taken, already pending, cancel, accept;
- agency not found, impersonation, signup.

Deliberately NOT here (still category-coded, said in the code): the reset
token (the front branches on `bad_request`), the superadmin wizard's slug /
email conflicts (told apart by the detail text), image 404s, the unbound
route 403 and the unseeded admin role (technical)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import ratelimit
from src.core.enums import Audience, InvitationStatus
from src.core.security import create_access_token
from tests.plugins.agency_plugin import MakeAgency, MakeAgentInvitation
from tests.plugins.agent_plugin import DEFAULT_PASSWORD, AuthHeaders, MakeAgent
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], email="admin@e1.io")


def _envelope(response: Response) -> tuple[str, dict[str, Any], str]:
    body = response.json()
    return body["code"], body["params"], body["detail"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- authentication -------------------------------------------------------------------


async def test_every_login_failure_shares_invalid_credentials(
    client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
) -> None:
    wrong = await client.post(
        "/auth/agent/login", json={"email": admin.email, "password": "not-the-one"}
    )
    unknown = await client.post(
        "/auth/agent/login", json={"email": "nobody@e1.io", "password": DEFAULT_PASSWORD}
    )
    pending = await make_expat_user(email="pending@e1.io", activated=False)
    not_activated = await client.post(
        "/auth/expat/login", json={"email": pending.email, "password": DEFAULT_PASSWORD}
    )
    for response in (wrong, unknown, not_activated):
        assert response.status_code == 401
        assert response.json() == {
            "detail": "Invalid credentials.",
            "code": "auth.invalid_credentials",
            "params": {},
        }


async def test_rejected_session_tokens_share_session_expired(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    gone = await make_agent(agency_id=admin.agency_id, email="gone@e1.io")
    gone.deactivated_at = datetime.now(UTC)
    await db_session.commit()
    expat = await make_expat_user(email="client@e1.io")
    expat_token = create_access_token(str(expat.id), Audience.EXPAT)
    agent_token = create_access_token(str(admin.id), Audience.AGENT)

    cases: dict[str, Response] = {
        "Missing authentication token.": await client.get("/auth/agent/me"),
        "Invalid or expired token.": await client.get(
            "/auth/agent/me", headers=_bearer("not-a-jwt")
        ),
        "Agent not found.": await client.get("/auth/agent/me", headers=agent_headers(gone)),
    }
    # The expat face answers the SAME code (one gesture: sign in again).
    expat_missing = await client.get("/auth/expat/me")
    # A token of the other face fails on the signature already.
    cross_face = await client.get("/auth/expat/me", headers=_bearer(agent_token))
    # Refresh: garbage, then an ACCESS token presented as a refresh one.
    refresh_garbage = await client.post("/auth/agent/refresh", json={"refresh_token": "x"})
    wrong_type = await client.post("/auth/expat/refresh", json={"refresh_token": expat_token})

    for detail, response in cases.items():
        assert response.status_code == 401, response.text
        assert response.json() == {
            "detail": detail,
            "code": "auth.session_expired",
            "params": {},
        }
    for response in (expat_missing, cross_face, refresh_garbage, wrong_type):
        assert response.status_code == 401, response.text
        code, params, _ = _envelope(response)
        assert (code, params) == ("auth.session_expired", {})


async def test_stale_mfa_token_sends_back_to_login(client: AsyncClient) -> None:
    """The step-2 token is not a session: its rejection carries the code the
    login screens branch on to drop back to step 1 (before E1 it answered
    the category, and the screen stayed on the code input)."""
    response = await client.post(
        "/auth/agent/2fa/verify", json={"mfa_token": "expired-or-forged", "code": "123456"}
    )
    assert response.status_code == 401
    code, params, detail = _envelope(response)
    assert (code, params) == ("auth.mfa_token_expired", {})
    assert detail == "Invalid or expired token."


# --- the permission matrix ------------------------------------------------------------


async def test_missing_permission_names_the_key(
    client: AsyncClient, make_agent: MakeAgent, agent_headers: AuthHeaders
) -> None:
    no_rights = await make_agent()  # an empty custom role
    response = await client.post(
        "/agencies/me/invitations",
        headers=agent_headers(no_rights),
        json={"email": "x@e1.io", "role_id": str(uuid.uuid4())},
    )
    assert response.status_code == 403
    assert response.json() == {
        "detail": "Missing permission.",
        "code": "permission.denied",
        "params": {"permission": "agent.manage"},
    }


async def test_provider_outside_its_allowlist_is_permission_denied(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    external_role = (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    provider = await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="ext@e1.io"
    )
    response = await client.get("/cases", headers=agent_headers(provider))
    assert response.status_code == 403
    code, params, detail = _envelope(response)
    assert (code, params) == ("permission.denied", {})
    assert detail == "External providers have no access to this resource yet."


# --- members --------------------------------------------------------------------------


async def test_unknown_member_is_member_not_found(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    h = agent_headers(admin)
    ghost = uuid.uuid4()
    responses = [
        await client.post(f"/agencies/me/members/{ghost}/deactivate", headers=h),
        await client.post(f"/agencies/me/members/{ghost}/reactivate", headers=h),
        await client.put(
            f"/agencies/me/members/{ghost}/seat-type", headers=h, json={"seat_type": "reader"}
        ),
        await client.post(f"/agencies/me/members/{ghost}/impersonate", headers=h),
    ]
    for response in responses:
        assert response.status_code == 404, response.text
        code, params, _ = _envelope(response)
        assert (code, params) == ("member.not_found", {})


async def test_last_manager_refusal_carries_its_member_code(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """THE fix of the wave: the offboarding dialog shows its dedicated
    screen on `member.last_manager` — the english detail stays the roles
    domain's, byte-identical."""
    response = await client.post(
        f"/agencies/me/members/{admin.id}/deactivate", headers=agent_headers(admin)
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": (
            "This operation would leave the agency without any manager "
            "(no agent holding agent.manage)."
        ),
        "code": "member.last_manager",
        "params": {},
    }


async def test_own_seat_type_flip_is_refused_with_its_code(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await client.put(
        f"/agencies/me/members/{admin.id}/seat-type",
        headers=agent_headers(admin),
        json={"seat_type": "reader"},
    )
    assert response.status_code == 403
    code, params, detail = _envelope(response)
    assert (code, params) == ("seat.self_change", {})
    assert detail == "You cannot modify your own seat type."


# --- invitations ----------------------------------------------------------------------


async def test_invitation_refusals_on_create(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    member_role = str(system_roles["member"].id)

    unknown_role = await client.post(
        "/agencies/me/invitations",
        headers=h,
        json={"email": "new@e1.io", "role_id": str(uuid.uuid4())},
    )
    assert unknown_role.status_code == 422
    assert _envelope(unknown_role)[:2] == ("invitation.role_invalid", {})

    # A platform role is refused with the SAME opaque code (never revealed).
    platform_role = await client.post(
        "/agencies/me/invitations",
        headers=h,
        json={"email": "new@e1.io", "role_id": str(system_roles["superadmin"].id)},
    )
    assert platform_role.status_code == 422
    assert _envelope(platform_role)[:2] == ("invitation.role_invalid", {})

    # One human = one agent account, whatever the agency (and another
    # agency keeps this one's trial seat cap out of the way).
    await make_agent(email="taken@e1.io")
    taken = await client.post(
        "/agencies/me/invitations", headers=h, json={"email": "taken@e1.io", "role_id": member_role}
    )
    assert taken.status_code == 409
    assert taken.json() == {
        "detail": "This email already has an agent account.",
        "code": "member.email_taken",
        "params": {"email": "taken@e1.io"},
    }

    first = await client.post(
        "/agencies/me/invitations", headers=h, json={"email": "twice@e1.io", "role_id": member_role}
    )
    assert first.status_code == 201, first.text
    again = await client.post(
        "/agencies/me/invitations", headers=h, json={"email": "twice@e1.io", "role_id": member_role}
    )
    assert again.status_code == 409
    assert again.json() == {
        "detail": "An invitation is already pending for this email.",
        "code": "invitation.already_pending",
        "params": {"email": "twice@e1.io"},
    }


async def test_invitation_cancel_refusals(
    client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    make_agent_invitation: MakeAgentInvitation,
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    missing = await client.delete(f"/agencies/me/invitations/{uuid.uuid4()}", headers=h)
    assert missing.status_code == 404
    assert _envelope(missing)[:2] == ("invitation.not_found", {})

    cancelled = await make_agent_invitation(
        agency_id=admin.agency_id,
        role_id=system_roles["member"].id,
        status=InvitationStatus.CANCELLED,
    )
    again = await client.delete(f"/agencies/me/invitations/{cancelled.id}", headers=h)
    assert again.status_code == 409
    assert _envelope(again)[:2] == ("invitation.not_pending", {})


async def test_invitation_accept_names_unknown_and_expired_apart(
    client: AsyncClient,
    admin: Agent,
    system_roles: dict[str, Role],
    make_agent_invitation: MakeAgentInvitation,
) -> None:
    """Same two refusals — and codes — as the client activation link. The
    english detail stays the historical one on both."""
    body = {"password": "MotDePasse1!", "first_name": "New", "last_name": "Member"}
    unknown = await client.post(
        "/agencies/invitations/accept", json={"token": "no-such-token", **body}
    )
    expired_invitation = await make_agent_invitation(
        agency_id=admin.agency_id,
        role_id=system_roles["member"].id,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    expired = await client.post(
        "/agencies/invitations/accept", json={"token": expired_invitation.token, **body}
    )
    for response, code in ((unknown, "invitation.invalid"), (expired, "invitation.expired")):
        assert response.status_code == 400
        assert response.json() == {
            "detail": "Invalid or expired invitation token.",
            "code": code,
            "params": {},
        }


# --- agency, impersonation ------------------------------------------------------------


async def test_unknown_agency_reuses_agency_not_found(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    superadmin = await make_agent(role=system_roles["superadmin"])
    response = await client.patch(
        f"/agencies/{uuid.uuid4()}/trial",
        headers=agent_headers(superadmin),
        json={"extend_days": 7},
    )
    assert response.status_code == 404
    assert response.json() == {
        "detail": "Agency not found.",
        "code": "agency.not_found",
        "params": {},
    }


async def test_impersonation_refusals(
    client: AsyncClient,
    admin: Agent,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    h = agent_headers(admin)
    self_target = await client.post(f"/agencies/me/members/{admin.id}/impersonate", headers=h)
    assert self_target.status_code == 422
    assert _envelope(self_target)[:2] == ("impersonation.self", {})

    stranger: ExpatUser = await make_expat_user(email="stranger@e1.io")
    not_a_client = await client.post(f"/expat-users/{stranger.id}/impersonate", headers=h)
    assert not_a_client.status_code == 404
    assert _envelope(not_a_client)[:2] == ("impersonation.client_not_found", {})

    # The superadmin switcher (same manager).
    superadmin = await make_agent(role=system_roles["superadmin"])
    sh = agent_headers(superadmin)
    own = await client.post(f"/agencies/{superadmin.agency_id}/enter", headers=sh)
    assert own.status_code == 422
    assert _envelope(own)[:2] == ("impersonation.same_agency", {})
    empty = await make_agency()
    no_admin = await client.post(f"/agencies/{empty.id}/enter", headers=sh)
    assert no_admin.status_code == 404
    assert _envelope(no_admin)[:2] == ("impersonation.no_admin", {})


# --- signup ---------------------------------------------------------------------------


@pytest.fixture
def _signup_harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Deterministic code, captured mail, a clean per-IP limiter."""
    monkeypatch.setattr("src.signup.signup_manager._generate_code", lambda: "123456")
    monkeypatch.setattr(
        "src.signup.signup_manager.send_email", lambda to, subject, text, html=None, **kw: None
    )
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.mark.usefixtures("_signup_harness")
async def test_signup_rate_limit_has_its_code(client: AsyncClient) -> None:
    last: Response | None = None
    for _ in range(16):  # the verify window allows 15 tries per IP
        last = await client.post("/signup/verify", json={"email": "x@e1.io", "code": "000000"})
    assert last is not None and last.status_code == 429
    assert last.json() == {
        "detail": "Too many attempts; retry later.",
        "code": "signup.too_many_attempts",
        "params": {},
    }


@pytest.mark.usefixtures("_signup_harness")
async def test_signup_cyrillic_agency_name_is_no_longer_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Flagged in the E1 report: a name written entirely in Cyrillic used to
    lose every letter of its slug and end on signup.agency_name_invalid. The
    slug is transliterated now (signup 01/10), and the signup goes through."""
    requested = await client.post("/signup", json={"email": "ru@e1.io", "lang": "ru"})
    assert requested.status_code == 200, requested.text
    verified = await client.post("/signup/verify", json={"email": "ru@e1.io", "code": "123456"})
    assert verified.status_code == 200, verified.text
    response = await client.post(
        "/signup/complete",
        json={
            "completion_token": verified.json()["completion_token"],
            "agency_name": "Агентство",
            "first_name": "Ivan",
            "last_name": "Petrov",
            "password": "MotDePasse1!",
            "language": "ru",
            "sectors": ["legal"],
        },
    )
    assert response.status_code == 200, response.text
    agency = (
        await db_session.execute(select(Agency).where(Agency.name == "Агентство"))
    ).scalar_one()
    assert agency.slug == "agentstvo"
