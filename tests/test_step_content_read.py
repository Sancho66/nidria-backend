"""Step content (Feature 2, V2) — the RGPD READ filter on the expat &
external faces. The content lives on the TEMPLATE step; the RIGHT to see it
lives on the DOSSIER (case_step_progress.responsible_agent_id).

These tests ARE the safety, hit over HTTP (never "is it displayed?"):
- the expat ALWAYS sees the content on its own dossier (+ download 200);
- a provider sees it ONLY on a step it is responsible for (+ download 200);
- a provider NOT responsible: content absent from the wire (None/[]) AND
  the direct download URL → 404 (server-side, not a byte served);
- THE CROSSING — same provider, SAME template step, two dossiers: 200 on
  the one it is responsible for, 404 on the other. This proves empirically
  that the INSTANCE column filters, not the template (the point-1 invariant).
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

ATTACH = {"file": ("guide.pdf", b"%PDF-1.4 step content")}
NOTE = "Merci de fournir le justificatif de domicile."


@pytest.fixture
def rc(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Owner", last_name="Agent")


@pytest_asyncio.fixture
async def external_role(db_session: AsyncSession, rbac_baseline: None) -> Role:
    return (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()


@pytest_asyncio.fixture
async def external(make_agent: MakeAgent, admin: Agent, external_role: Role) -> Agent:
    return await make_agent(
        agency_id=admin.agency_id,
        role=external_role,
        is_external=True,
        email="lawyer@ext.com",
        first_name="Robert",
        last_name="Lawyer",
    )


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


# --- scenario builders ---------------------------------------------------------------


async def _template_with_content(rc: AsyncClient, ah: dict[str, str]) -> tuple[str, str, str]:
    """A journey template with one step carrying a content_note + one
    attachment. Returns (template_id, template_step_id, attachment_id).
    This content is shared by every case using the template."""
    tid = (await rc.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    sid = (await rc.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Collecte"})).json()[
        "id"
    ]
    await rc.patch(f"/journeys/{tid}/steps/{sid}", headers=ah, json={"content_note": NOTE})
    aid = (
        await rc.post(f"/journeys/{tid}/steps/{sid}/attachments", headers=ah, files=ATTACH)
    ).json()["id"]
    return tid, sid, aid


async def _case_with_journey(
    rc: AsyncClient,
    ah: dict[str, str],
    make_client_case: MakeClientCase,
    admin: Agent,
    principal: ExpatUser,
    tid: str,
) -> tuple[ClientCase, str]:
    """A case owned by `principal`, with the template assigned. Returns
    (case, progress_id) — the progress_id is the case-step INSTANCE id."""
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    steps = (
        await rc.post(f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid})
    ).json()
    return case, steps[0]["id"]


async def _make_responsible(
    rc: AsyncClient, ah: dict[str, str], case_id: uuid.UUID, pid: str, external: Agent
) -> None:
    """Assign the provider to the case, then name it responsible for the
    step (the invariant requires the assignment first)."""
    assigned = await rc.post(
        f"/cases/{case_id}/external-assignments", headers=ah, json={"agent_id": str(external.id)}
    )
    assert assigned.status_code == 201, assigned.text
    r = await rc.put(
        f"/cases/{case_id}/steps/{pid}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(external.id)},
    )
    assert r.status_code == 200, r.text


# --- expat: always sees content ------------------------------------------------------


async def test_expat_always_sees_content_and_downloads(
    rc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    tid, _sid, aid = await _template_with_content(rc, ah)
    case, pid = await _case_with_journey(rc, ah, make_client_case, admin, expat, tid)

    detail = (await rc.get(f"/expat/cases/{case.id}", headers=eh)).json()
    step = detail["timeline"][0]
    assert step["content_note"] == NOTE
    assert [a["id"] for a in step["attachments"]] == [aid]

    dl = await rc.get(f"/expat/cases/{case.id}/steps/{pid}/attachments/{aid}/download", headers=eh)
    assert dl.status_code == 200
    assert dl.content == b"%PDF-1.4 step content"


async def test_expat_foreign_case_download_404(
    rc: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """A different expat's dossier → 404 (cross-dossier ownership), proven
    at the endpoint, not by hiding the link."""
    ah = agent_headers(admin)
    tid, _sid, aid = await _template_with_content(rc, ah)
    case, pid = await _case_with_journey(rc, ah, make_client_case, admin, expat, tid)
    stranger = await make_expat_user(email="stranger@example.com")
    dl = await rc.get(
        f"/expat/cases/{case.id}/steps/{pid}/attachments/{aid}/download",
        headers=expat_headers(stranger),
    )
    assert dl.status_code == 404


# --- external: responsible sees, non-responsible blocked -----------------------------


async def test_external_responsible_sees_content_and_downloads(
    rc: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah, h = agent_headers(admin), agent_headers(external)
    tid, _sid, aid = await _template_with_content(rc, ah)
    case, pid = await _case_with_journey(rc, ah, make_client_case, admin, expat, tid)
    await _make_responsible(rc, ah, case.id, pid, external)

    detail = (await rc.get(f"/external/cases/{case.id}", headers=h)).json()
    step = detail["timeline"][0]
    assert step["content_note"] == NOTE
    assert [a["id"] for a in step["attachments"]] == [aid]

    dl = await rc.get(
        f"/external/cases/{case.id}/steps/{pid}/attachments/{aid}/download", headers=h
    )
    assert dl.status_code == 200
    assert dl.content == b"%PDF-1.4 step content"


async def test_external_not_responsible_content_hidden_and_download_404(
    rc: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The provider is ASSIGNED to the case (it can read the timeline) but
    NOT responsible for the step → content absent FROM THE WIRE (None/[])
    AND the direct download URL → 404. Server-side, not front-masked."""
    ah, h = agent_headers(admin), agent_headers(external)
    tid, _sid, aid = await _template_with_content(rc, ah)
    case, pid = await _case_with_journey(rc, ah, make_client_case, admin, expat, tid)
    # Assigned to the case, but NEVER named responsible for the step.
    assigned = await rc.post(
        f"/cases/{case.id}/external-assignments", headers=ah, json={"agent_id": str(external.id)}
    )
    assert assigned.status_code == 201

    detail = (await rc.get(f"/external/cases/{case.id}", headers=h)).json()
    step = detail["timeline"][0]
    assert step["content_note"] is None
    assert step["attachments"] == []
    # The note text never appears ANYWHERE in the serialized response.
    assert NOTE not in str(detail)

    dl = await rc.get(
        f"/external/cases/{case.id}/steps/{pid}/attachments/{aid}/download", headers=h
    )
    assert dl.status_code == 404  # never a byte served


# --- THE CROSSING: same provider, same template step, two dossiers -------------------


async def test_crossing_responsible_on_X_not_on_Z(
    rc: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """THE decisive test. ONE provider, ONE template step (same content,
    same attachment id), TWO dossiers X and Z. Responsible on X, merely
    assigned on Z. → content + download 200 on X, None/[] + 404 on Z.
    Same template_step_id, opposite outcomes ⇒ it is the case-INSTANCE
    column (responsible_agent_id) that filters, not the template."""
    ah, h = agent_headers(admin), agent_headers(external)
    tid, sid, aid = await _template_with_content(rc, ah)

    other = await make_expat_user(email="other@example.com")
    case_x, pid_x = await _case_with_journey(rc, ah, make_client_case, admin, expat, tid)
    case_z, pid_z = await _case_with_journey(rc, ah, make_client_case, admin, other, tid)

    # Responsible on X; on Z only assigned (timeline-visible, not step-owner).
    await _make_responsible(rc, ah, case_x.id, pid_x, external)
    assigned_z = await rc.post(
        f"/cases/{case_z.id}/external-assignments", headers=ah, json={"agent_id": str(external.id)}
    )
    assert assigned_z.status_code == 201

    # Both timeline steps point at the SAME template step + SAME attachment.
    step_x = (await rc.get(f"/external/cases/{case_x.id}", headers=h)).json()["timeline"][0]
    step_z = (await rc.get(f"/external/cases/{case_z.id}", headers=h)).json()["timeline"][0]
    assert step_x["name"] == step_z["name"] == "Collecte"  # same template step
    assert pid_x != pid_z  # but distinct case-step INSTANCES

    # X (responsible): content present + download served.
    assert step_x["content_note"] == NOTE
    assert [a["id"] for a in step_x["attachments"]] == [aid]
    dl_x = await rc.get(
        f"/external/cases/{case_x.id}/steps/{pid_x}/attachments/{aid}/download", headers=h
    )
    assert dl_x.status_code == 200

    # Z (not responsible): content hidden + download refused — SAME aid.
    assert step_z["content_note"] is None
    assert step_z["attachments"] == []
    dl_z = await rc.get(
        f"/external/cases/{case_z.id}/steps/{pid_z}/attachments/{aid}/download", headers=h
    )
    assert dl_z.status_code == 404

    _ = sid  # the template step id is shared; the outcomes diverge by dossier
