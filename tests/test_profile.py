"""User settings bloc 1: own names, profile picture, logged-in password
change — both faces.

Covers: (a) name PATCH per face (expat identity is GLOBAL across
agencies, deliberate); (b) the avatar pipeline (upload → 512px JPEG
square, bad type 422, oversize 413, corrupt 422, backend-served read,
delete = initials fallback); (c) change-password (wrong current 403,
success revokes every refresh token); (d) the impersonation locks
(expat face read-only, agent change-password in the session-lifecycle
denylist); (e) isolation (client avatar visible to HIS agency only,
agent avatar inside the agency only)."""

from io import BytesIO

import pytest
import pytest_asyncio
from httpx import AsyncClient
from PIL import Image

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import storage
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


def _png(size: tuple[int, int] = (800, 600)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, (30, 90, 200)).save(buf, format="PNG")
    return buf.getvalue()


# --- (a) names -------------------------------------------------------------------------


async def test_patch_names_both_faces(
    client: AsyncClient,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    patched = await client.patch(
        "/profile/agent", headers=headers, json={"first_name": "Nadia", "last_name": "Renard"}
    )
    assert patched.status_code == 200, patched.text
    me = await client.get("/auth/agent/me", headers=headers)
    assert (me.json()["first_name"], me.json()["last_name"]) == ("Nadia", "Renard")

    # Expat: the SAME global identity everywhere (deliberate — the client
    # manages their own name, every agency holding a case sees it).
    expat = await make_expat_user()
    eh = _expat_headers(expat)
    patched = await client.patch("/profile/expat", headers=eh, json={"first_name": "Youssef"})
    assert patched.status_code == 200
    me = await client.get("/auth/expat/me", headers=eh)
    assert me.json()["first_name"] == "Youssef"
    assert me.json()["last_name"] == expat.last_name  # partial patch: untouched

    # Empty names refused by the schema.
    assert (
        await client.patch("/profile/agent", headers=headers, json={"first_name": ""})
    ).status_code == 422


# --- (b) avatar pipeline -----------------------------------------------------------------


async def test_avatar_upload_read_delete(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    uploaded = await client.post(
        "/profile/agent/avatar",
        headers=headers,
        files={"file": ("me.png", _png(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_avatar"] is True
    assert (await client.get("/auth/agent/me", headers=headers)).json()["has_avatar"] is True

    # Backend-served read: normalized 512px JPEG square, private cache.
    read = await client.get(f"/profile/agent/avatar/{admin.id}", headers=headers)
    assert read.status_code == 200
    assert read.headers["content-type"] == "image/jpeg"
    image = Image.open(BytesIO(read.content))
    assert image.size == (512, 512) and image.format == "JPEG"

    # Re-upload overwrites the SAME path (no orphan blobs).
    assert len([p for p in storage.mock_store if p.startswith("avatars/agent/")]) == 1
    again = await client.post(
        "/profile/agent/avatar",
        headers=headers,
        files={"file": ("me2.png", _png((300, 300)), "image/png")},
    )
    assert again.status_code == 200
    assert len([p for p in storage.mock_store if p.startswith("avatars/agent/")]) == 1

    # Delete: back to initials, read is a 404, storage is clean.
    deleted = await client.delete("/profile/agent/avatar", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["has_avatar"] is False
    assert (
        await client.get(f"/profile/agent/avatar/{admin.id}", headers=headers)
    ).status_code == 404
    assert [p for p in storage.mock_store if p.startswith("avatars/agent/")] == []


async def test_avatar_refusals(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    bad_type = await client.post(
        "/profile/agent/avatar",
        headers=headers,
        files={"file": ("x.gif", _png(), "image/gif")},
    )
    assert bad_type.status_code == 422
    assert bad_type.json()["code"] == "profile.avatar_bad_type"

    too_large = await client.post(
        "/profile/agent/avatar",
        headers=headers,
        files={"file": ("x.jpg", b"x" * (2 * 1024 * 1024 + 1), "image/jpeg")},
    )
    assert too_large.status_code == 413
    assert too_large.json()["code"] == "profile.avatar_too_large"

    corrupt = await client.post(
        "/profile/agent/avatar",
        headers=headers,
        files={"file": ("x.jpg", b"not an image at all", "image/jpeg")},
    )
    assert corrupt.status_code == 422
    assert corrupt.json()["code"] == "profile.avatar_invalid"


# --- (c) logged-in password change -------------------------------------------------------


async def test_change_password_verifies_current_and_revokes_sessions(
    client: AsyncClient, make_agent: MakeAgent, system_roles: dict[str, Role]
) -> None:
    agent = await make_agent(role=system_roles["admin"], email="pwd@example.com")
    login = await client.post(
        "/auth/agent/login", json={"email": agent.email, "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 200
    pair = login.json()
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    wrong = await client.post(
        "/auth/agent/change-password",
        headers=headers,
        json={"current_password": "not-the-password", "new_password": "brand-new-pass-1"},
    )
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "auth.wrong_password"

    ok = await client.post(
        "/auth/agent/change-password",
        headers=headers,
        json={"current_password": DEFAULT_PASSWORD, "new_password": "brand-new-pass-1"},
    )
    assert ok.status_code == 200, ok.text

    # Other sessions fall: the pre-change refresh token is dead.
    refreshed = await client.post(
        "/auth/agent/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert refreshed.status_code == 401
    # Old password dead, new one lives; the current ACCESS token still works.
    assert (
        await client.post(
            "/auth/agent/login", json={"email": agent.email, "password": DEFAULT_PASSWORD}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/auth/agent/login", json={"email": agent.email, "password": "brand-new-pass-1"}
        )
    ).status_code == 200
    assert (await client.get("/auth/agent/me", headers=headers)).status_code == 200


async def test_change_password_expat_face(
    client: AsyncClient, make_expat_user: MakeExpatUser
) -> None:
    expat = await make_expat_user(email="pwd-client@example.com")
    eh = _expat_headers(expat)
    ok = await client.post(
        "/auth/expat/change-password",
        headers=eh,
        json={"current_password": "password123", "new_password": "fresh-client-pw1"},
    )
    assert ok.status_code == 200, ok.text
    assert (
        await client.post(
            "/auth/expat/login", json={"email": expat.email, "password": "fresh-client-pw1"}
        )
    ).status_code == 200


# --- (d) impersonation locks --------------------------------------------------------------


async def test_profile_writes_blocked_under_impersonation(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    # Expat face: the point-12 read-only mask covers every profile write.
    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    issued = await client.post(f"/expat-users/{expat.id}/impersonate", headers=headers)
    mask = {"Authorization": f"Bearer {issued.json()['access_token']}"}
    for method, url, kwargs in (
        ("PATCH", "/profile/expat", {"json": {"first_name": "Forged"}}),
        ("POST", "/profile/expat/avatar", {"files": {"file": ("x.png", _png(), "image/png")}}),
        ("DELETE", "/profile/expat/avatar", {}),
    ):
        response = await client.request(method, url, headers=mask, **kwargs)
        assert response.status_code == 403, (method, url)
        assert response.json()["code"] == "impersonation.read_only"

    # Agent face: changing the TARGET's password = session lifecycle, denylisted.
    colleague = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    issued = await client.post(f"/agencies/me/members/{colleague.id}/impersonate", headers=headers)
    agent_mask = {"Authorization": f"Bearer {issued.json()['access_token']}"}
    denied = await client.post(
        "/auth/agent/change-password",
        headers=agent_mask,
        json={"current_password": DEFAULT_PASSWORD, "new_password": "hijacked-pass-1"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "impersonation.denied"


# --- (e) isolation --------------------------------------------------------------------------


async def test_avatar_isolation(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    # A client of agency A with an avatar.
    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    eh = _expat_headers(expat)
    assert (
        await client.post(
            "/profile/expat/avatar",
            headers=eh,
            files={"file": ("c.png", _png(), "image/png")},
        )
    ).status_code == 200
    assert (await client.get("/profile/expat/avatar", headers=eh)).status_code == 200

    # Agency A (holds the case) sees it; agency B does not.
    stranger = await make_agent(role=system_roles["admin"])  # its own other agency
    assert (
        await client.get(f"/profile/clients/{expat.id}/avatar", headers=agent_headers(admin))
    ).status_code == 200
    assert (
        await client.get(f"/profile/clients/{expat.id}/avatar", headers=agent_headers(stranger))
    ).status_code == 404

    # Agent avatars: same agency yes, cross-agency no.
    colleague = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    assert (
        await client.post(
            "/profile/agent/avatar",
            headers=agent_headers(colleague),
            files={"file": ("a.png", _png(), "image/png")},
        )
    ).status_code == 200
    assert (
        await client.get(f"/profile/agent/avatar/{colleague.id}", headers=agent_headers(admin))
    ).status_code == 200
    assert (
        await client.get(f"/profile/agent/avatar/{colleague.id}", headers=agent_headers(stranger))
    ).status_code == 404
