"""Agency cover banner battery (branding, same family as the logo):
admin upload/read/delete (agency.manage gate), the cover flavor of the
shared image pipeline (center-crop 4:1, width bounded, no upscale, its
own 5 MiB cap), scoped authenticated reads on the three faces (expat of
another agency: 404; external provider: own agency via the allowlisted
/agencies/me read), and NO public route."""

from io import BytesIO

import pytest
import pytest_asyncio
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


def _photo_jpeg(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (90, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


async def _upload(client: AsyncClient, headers: dict[str, str], raw: bytes) -> object:
    return await client.post(
        "/agencies/me/cover",
        headers=headers,
        files={"file": ("cover.jpg", raw, "image/jpeg")},
    )


# --- (a) + (e) pipeline: centered 4:1 crop, bounded width, no upscale ---------------------


async def test_admin_upload_reads_a_4_1_banner(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    uploaded = await _upload(client, headers, _photo_jpeg(3000, 3000))
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["has_cover"] is True
    assert (await client.get("/agencies/me", headers=headers)).json()["has_cover"] is True

    read = await client.get("/agencies/me/cover", headers=headers)
    assert read.status_code == 200
    assert read.headers["content-type"] == "image/jpeg"
    assert read.headers["cache-control"] == "private, max-age=300"
    image = Image.open(BytesIO(read.content))
    assert image.format == "JPEG"
    assert image.size == (2560, 640)  # capped width, 4:1 center crop

    # A smaller landscape is cropped, never upscaled: 2000x1000 → 2000x500.
    assert (await _upload(client, headers, _photo_jpeg(2000, 1000))).status_code == 200
    read = await client.get("/agencies/me/cover", headers=headers)
    assert Image.open(BytesIO(read.content)).size == (2000, 500)


async def test_extreme_portrait_cropped_without_crash(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    uploaded = await _upload(client, headers, _photo_jpeg(200, 4000))
    assert uploaded.status_code == 200, uploaded.text
    read = await client.get("/agencies/me/cover", headers=headers)
    assert Image.open(BytesIO(read.content)).size == (200, 50)  # 4:1 from the source width


async def test_cover_has_its_own_5mib_cap(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    import os

    headers = agent_headers(admin)

    def _noise_png(side: int) -> bytes:
        buf = BytesIO()
        Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(buf, format="PNG")
        return buf.getvalue()

    # Heavier than the 2 MiB logo cap but under 5 MiB: accepted (photos).
    mid = _noise_png(1100)  # ~3.6 MiB of incompressible noise
    assert 2 * 1024 * 1024 < len(mid) < 5 * 1024 * 1024
    accepted = await client.post(
        "/agencies/me/cover", headers=headers, files={"file": ("c.png", mid, "image/png")}
    )
    assert accepted.status_code == 200, accepted.text

    big = _noise_png(1500)  # ~6.7 MiB
    assert len(big) > 5 * 1024 * 1024
    rejected = await client.post(
        "/agencies/me/cover", headers=headers, files={"file": ("c.png", big, "image/png")}
    )
    assert rejected.status_code == 413
    assert rejected.json()["code"] == "agency.cover_too_large"


# --- (b) the write gate -----------------------------------------------------------------


async def test_non_admin_cannot_touch_cover(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    headers = agent_headers(member)
    assert (await _upload(client, headers, _photo_jpeg(800, 200))).status_code == 403
    assert (await client.delete("/agencies/me/cover", headers=headers)).status_code == 403
    # Reading stays open to every member (the app shell shows it).
    assert (await client.get("/agencies/me/cover", headers=headers)).status_code == 404


# --- (c) scoped reads on the client + provider faces -----------------------------------------


async def test_expat_read_scoped_to_their_agencies(
    client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    assert (await _upload(client, agent_headers(admin), _photo_jpeg(1600, 400))).status_code == 200

    insider = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=insider.id)
    ok = await client.get(
        f"/expat/agencies/{admin.agency_id}/cover", headers=_expat_headers(insider)
    )
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/jpeg"
    assert ok.headers["cache-control"] == "private, max-age=300"

    # The client-space summary carries the branding context.
    summary = (await client.get("/expat/cases", headers=_expat_headers(insider))).json()
    assert summary[0]["agency"]["has_cover"] is True

    # An expat with NO case at this agency reads nothing.
    stranger_agency_admin = await make_agent(role=system_roles["admin"])
    stranger = await make_expat_user()
    await make_client_case(
        agency_id=stranger_agency_admin.agency_id, principal_expat_user_id=stranger.id
    )
    denied = await client.get(
        f"/expat/agencies/{admin.agency_id}/cover", headers=_expat_headers(stranger)
    )
    assert denied.status_code == 404


async def test_external_provider_reads_own_agency_cover(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    assert (await _upload(client, agent_headers(admin), _photo_jpeg(1600, 400))).status_code == 200
    external_role = (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    external = await make_agent(agency_id=admin.agency_id, role=external_role, is_external=True)
    expat = await make_expat_user()
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    assigned = await client.post(
        f"/cases/{case.id}/external-assignments",
        headers=agent_headers(admin),
        json={"agent_id": str(external.id)},
    )
    assert assigned.status_code == 201, assigned.text

    # Own-agency read through the allowlisted /agencies/me route (like the
    # logo), and the portal context carries has_cover.
    read = await client.get("/agencies/me/cover", headers=agent_headers(external))
    assert read.status_code == 200
    assert read.headers["content-type"] == "image/jpeg"
    portal = (await client.get("/external/cases", headers=agent_headers(external))).json()
    assert portal[0]["agency"]["has_cover"] is True


# --- (d) deletion + no public route -----------------------------------------------------------


async def test_delete_cover_and_no_public_route(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders, db_session: AsyncSession
) -> None:
    headers = agent_headers(admin)
    assert (await _upload(client, headers, _photo_jpeg(1600, 400))).status_code == 200

    from shared.models.agency import Agency

    slug = (await db_session.get(Agency, admin.agency_id)).slug
    # NO public route for the cover: the path does not even exist (the
    # logo's login-page exception was NOT extended to the banner).
    assert (await client.get(f"/public/agencies/{slug}/cover")).status_code == 404

    deleted = await client.delete("/agencies/me/cover", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["has_cover"] is False
    assert (await client.get("/agencies/me/cover", headers=headers)).status_code == 404
