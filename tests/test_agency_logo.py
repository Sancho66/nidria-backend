"""Agency logo battery: admin upload/read/delete (agency.manage gate),
scoped authenticated reads (expat of another agency: 404), THE assumed
public exception by slug (image only, no metadata, same 404 for unknown
slug and logo-less agency), and the logo flavor of the shared image
pipeline (no forced square, PNG kept on alpha, ratio intact)."""

from io import BytesIO

import pytest
import pytest_asyncio
from httpx import AsyncClient
from PIL import Image

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core.enums import Audience
from src.core.security import create_access_token
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _expat_headers(expat: ExpatUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(expat.id), Audience.EXPAT)}"}


def _rect_alpha_png(width: int = 2000, height: int = 500) -> bytes:
    """A wide transparent-background logo, the realistic shape."""
    buf = BytesIO()
    Image.new("RGBA", (width, height), (10, 40, 160, 120)).save(buf, format="PNG")
    return buf.getvalue()


def _rect_opaque_jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (1600, 400), (200, 30, 60)).save(buf, format="JPEG")
    return buf.getvalue()


# --- (a) + (e) admin lifecycle, logo flavor of the pipeline -----------------------------


async def test_admin_upload_read_delete_logo(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    uploaded = await client.post(
        "/agencies/me/logo",
        headers=headers,
        files={"file": ("logo.png", _rect_alpha_png(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_logo"] is True
    assert (await client.get("/agencies/me", headers=headers)).json()["has_logo"] is True

    # (e) alpha survives as PNG, width bounded to 1024, RATIO intact
    # (2000x500 → 1024x256), never a forced square.
    read = await client.get("/agencies/me/logo", headers=headers)
    assert read.status_code == 200
    assert read.headers["content-type"] == "image/png"
    image = Image.open(BytesIO(read.content))
    assert image.format == "PNG" and image.mode == "RGBA"
    assert image.size == (1024, 256)

    # Same-format re-upload (PNG→PNG): SAME storage path — must replace,
    # not 500 (Supabase refuses same-path overwrites; delete-first fix).
    replaced = await client.post(
        "/agencies/me/logo",
        headers=headers,
        files={"file": ("logo2.png", _rect_alpha_png(1200, 300), "image/png")},
    )
    assert replaced.status_code == 200, replaced.text
    read = await client.get("/agencies/me/logo", headers=headers)
    assert Image.open(BytesIO(read.content)).size == (1024, 256)

    # Opaque re-upload lands as JPEG and replaces the PNG blob.
    swapped = await client.post(
        "/agencies/me/logo",
        headers=headers,
        files={"file": ("logo.jpg", _rect_opaque_jpeg(), "image/jpeg")},
    )
    assert swapped.status_code == 200
    read = await client.get("/agencies/me/logo", headers=headers)
    assert read.headers["content-type"] == "image/jpeg"
    assert Image.open(BytesIO(read.content)).size == (1024, 256)

    deleted = await client.delete("/agencies/me/logo", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["has_logo"] is False
    assert (await client.get("/agencies/me/logo", headers=headers)).status_code == 404


# --- (b) the write gate -----------------------------------------------------------------


async def test_non_admin_cannot_touch_logo(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    headers = agent_headers(member)
    upload = await client.post(
        "/agencies/me/logo",
        headers=headers,
        files={"file": ("l.png", _rect_alpha_png(400, 100), "image/png")},
    )
    assert upload.status_code == 403  # agency.manage missing
    assert (await client.delete("/agencies/me/logo", headers=headers)).status_code == 403
    # Reading stays open to every member (the app shell shows it).
    assert (await client.get("/agencies/me/logo", headers=headers)).status_code == 404


# --- (c) scoped authenticated reads --------------------------------------------------------


async def test_expat_read_scoped_to_their_agencies(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    upload = await client.post(
        "/agencies/me/logo",
        headers=agent_headers(admin),
        files={"file": ("l.png", _rect_alpha_png(600, 200), "image/png")},
    )
    assert upload.status_code == 200

    insider = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=insider.id)
    ok = await client.get(
        f"/expat/agencies/{admin.agency_id}/logo", headers=_expat_headers(insider)
    )
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/png"

    # The client-space summary carries the branding context.
    summary = (await client.get("/expat/cases", headers=_expat_headers(insider))).json()
    assert summary[0]["agency"]["has_logo"] is True
    assert summary[0]["agency"]["slug"]

    # An expat with NO case at this agency reads nothing.
    stranger_agency_admin = await make_agent(role=system_roles["admin"])
    stranger = await make_expat_user()
    await make_client_case(
        agency_id=stranger_agency_admin.agency_id, principal_expat_user_id=stranger.id
    )
    denied = await client.get(
        f"/expat/agencies/{admin.agency_id}/logo", headers=_expat_headers(stranger)
    )
    assert denied.status_code == 404


# --- (d) THE public exception ---------------------------------------------------------------


async def test_public_logo_by_slug(
    client: AsyncClient,
    db_session,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    upload = await client.post(
        "/agencies/me/logo",
        headers=agent_headers(admin),
        files={"file": ("l.png", _rect_alpha_png(600, 200), "image/png")},
    )
    assert upload.status_code == 200
    from shared.models.agency import Agency

    slug = (await db_session.get(Agency, admin.agency_id)).slug

    public = await client.get(f"/public/agencies/{slug}/logo")  # NO auth header
    assert public.status_code == 200
    assert public.headers["content-type"] == "image/png"
    assert public.headers["cache-control"] == "public, max-age=3600"
    Image.open(BytesIO(public.content))  # the body IS the image, nothing else

    # Unknown slug and logo-less agency answer the SAME 404 shape.
    unknown = await client.get("/public/agencies/does-not-exist/logo")
    assert unknown.status_code == 404
    await client.delete("/agencies/me/logo", headers=agent_headers(admin))
    empty = await client.get(f"/public/agencies/{slug}/logo")
    assert empty.status_code == 404
    assert unknown.json() == empty.json()  # no enumeration signal, no metadata
