"""Per-case external scoping (wave B) — THE RGPD wave. The tests ARE the
safety: per-case isolation (assigned → 200, not assigned → 404 on every
portal route), no internal content leaks into the reduced external view
(notes/activity/contacts/staff/requirement VALUE), the wave-A guard still
denies the internal /cases/* surface, unassigning cuts access at once,
and permission never grants access without the assignment scoping."""

import uuid

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core.rbac.enforcement import EXTERNAL_AGENT_ALLOWLIST
from src.main import app
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCaseNote, MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

PDF = ("p.pdf", b"%PDF-1.4 fake", "application/pdf")


@pytest.fixture
def b_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
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
        agency_id=admin.agency_id, role=external_role, is_external=True, email="lawyer@ext.com"
    )


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


async def _case(make_client_case: MakeClientCase, admin: Agent, expat: ExpatUser) -> ClientCase:
    return await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )


async def _assign(
    client: AsyncClient, ah: dict[str, str], case_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    r = await client.post(
        f"/cases/{case_id}/external-assignments", headers=ah, json={"agent_id": str(agent_id)}
    )
    assert r.status_code == 201, r.text


# The {case_id} portal route TEMPLATES — the single source for both the
# "unassigned → 404 on EACH" battery and the structural completeness
# check. A new portal route must be added here (or completeness fails).
PORTAL_CASE_ROUTES: list[tuple[str, str]] = [
    ("GET", "/external/cases/{case_id}"),
    ("GET", "/external/cases/{case_id}/documents"),
    ("GET", "/external/cases/{case_id}/documents/{document_id}/download"),
    ("POST", "/external/cases/{case_id}/requirements/{requirement_id}/document"),
    ("GET", "/external/cases/{case_id}/steps/{progress_id}/comments"),
    ("POST", "/external/cases/{case_id}/steps/{progress_id}/comments"),
    ("PATCH", "/external/cases/{case_id}/steps/{progress_id}/comments/{comment_id}"),
    ("DELETE", "/external/cases/{case_id}/steps/{progress_id}/comments/{comment_id}"),
]


def _fill(template: str, case_id: uuid.UUID) -> str:
    path = template.replace("{case_id}", str(case_id))
    for param in ("{document_id}", "{requirement_id}", "{progress_id}", "{comment_id}"):
        path = path.replace(param, str(uuid.uuid4()))
    return path


def _body(method: str, template: str) -> dict:
    if method == "POST" and template.endswith("/document"):
        return {"files": {"file": PDF}}
    if method in {"POST", "PATCH"}:
        return {"json": {"body": "x"}}
    return {}


# --- THE ISOLATION TEST (per-case 404, the RGPD core) --------------------------------


async def test_unassigned_external_404_on_every_portal_route(
    b_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await _case(make_client_case, admin, expat)
    h = agent_headers(external)  # authenticated external, NOT assigned to this case
    for method, template in PORTAL_CASE_ROUTES:
        path = _fill(template, case.id)
        resp = await b_client.request(method, path, headers=h, **_body(method, template))
        assert resp.status_code == 404, f"{method} {template} → {resp.status_code} (expected 404)"
    # The list route returns an empty set (no assignment), not the case.
    listing = await b_client.get("/external/cases", headers=h)
    assert listing.status_code == 200
    assert listing.json() == []


async def test_assigned_to_d_but_not_d2(
    b_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah, h = agent_headers(admin), agent_headers(external)
    d = await _case(make_client_case, admin, expat)
    other_expat = await make_expat_user(email="other@example.com")
    d2 = await _case(make_client_case, admin, other_expat)
    await _assign(b_client, ah, d.id, external.id)  # assigned to D only

    assert (await b_client.get(f"/external/cases/{d.id}", headers=h)).status_code == 200
    assert (await b_client.get(f"/external/cases/{d2.id}", headers=h)).status_code == 404
    listed = (await b_client.get("/external/cases", headers=h)).json()
    assert {c["id"] for c in listed} == {str(d.id)}  # only D, never D2


# --- structural completeness: every portal route is allowlisted + tested -------------


def test_every_external_portal_route_is_allowlisted_and_covered() -> None:
    declared = {
        (m, r.path)
        for r in app.routes
        if isinstance(r, APIRoute) and r.path.startswith("/external/")
        for m in (r.methods or set())
        if m not in {"HEAD", "OPTIONS"}
    }
    # (1) every declared portal route is in the guard allowlist — nothing
    # reachable by an external that the guard didn't intend.
    assert declared <= EXTERNAL_AGENT_ALLOWLIST, (
        f"unlisted: {sorted(declared - EXTERNAL_AGENT_ALLOWLIST)}"
    )
    # (2) every {case_id} portal route is exercised by the unassigned-404
    # battery — a new portal route forces a new 404 case here.
    declared_case = {(m, p) for (m, p) in declared if "{case_id}" in p}
    assert declared_case == set(PORTAL_CASE_ROUTES), (
        f"portal routes not covered by the 404 battery: {declared_case ^ set(PORTAL_CASE_ROUTES)}"
    )


# --- non-leak of internal content (the exact-keys assertion) -------------------------


async def _setup_rich_case(
    b_client: AsyncClient,
    ah: dict[str, str],
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_case_note: MakeCaseNote,
) -> tuple[ClientCase, str]:
    """Case with a journey (one active step + a base_field requirement),
    plus internal notes — the stuff that must NOT leak to an external."""
    case = await _case(make_client_case, admin, expat)
    tid = (await b_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    step = (
        await b_client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Collecte"})
    ).json()
    await b_client.post(
        f"/journeys/{tid}/steps/{step['id']}/requirements",
        headers=ah,
        json={"kind": "base_field", "reference": "passport_number", "scope": "principal"},
    )
    steps = (
        await b_client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    pid = steps[0]["id"]
    await b_client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"}
    )
    # Fill the client's passport value — the external must STILL not see it.
    persons = (await b_client.get(f"/cases/{case.id}", headers=ah)).json()["persons"]
    principal = next(p for p in persons if p["kind"] == "principal")
    await b_client.patch(
        f"/cases/{case.id}/persons/{principal['id']}", headers=ah, json={"passport_number": "AB123"}
    )
    await make_case_note(case=case, body="internal", is_confidential=False)
    await make_case_note(case=case, body="secret", is_confidential=True)
    return case, pid


async def test_external_detail_leaks_no_internal_content(
    b_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_case_note: MakeCaseNote,
    agent_headers: AuthHeaders,
) -> None:
    ah, h = agent_headers(admin), agent_headers(external)
    case, _pid = await _setup_rich_case(
        b_client, ah, admin, expat, make_client_case, make_case_note
    )
    await _assign(b_client, ah, case.id, external.id)

    detail = (await b_client.get(f"/external/cases/{case.id}", headers=h)).json()
    # EXACT top-level keys — no notes / external_contacts / persons /
    # activity / custom_field_definitions.
    assert set(detail.keys()) == {
        "id",
        "agency",
        "principal",
        "origin_country",
        "dest_country",
        "status",
        "steps_done",
        "steps_total",
        "created_at",
        "updated_at",
        "referent",
        "timeline",
    }
    assert detail["referent"]["email"] == admin.email  # the agency contact IS exposed
    # The case holder's NAME is exposed (a provider must know who they work
    # for) — name ONLY, no email, no sensitive value.
    assert detail["principal"] == {"first_name": "Marie", "last_name": "Curie"}
    step = detail["timeline"][0]
    assert set(step.keys()) == {
        "progress_id",
        "name",
        "position",
        "status",
        "estimated_days",
        "completed_at",
        "blocked_by",
        "responsible",
        "completion_mode",
        "comment_count",
        "counter",
        "requirements",
    }
    req = step["requirements"][0]
    # EXACT requirement keys — NO "value" (the client's passport number).
    assert set(req.keys()) == {
        "id",
        "kind",
        "reference",
        "scope",
        "status",
        "person_label",
        "document_id",
    }
    assert "value" not in req
    assert req["status"] == "provided"  # the external sees IT'S provided…
    assert req["person_label"] == "Marie Curie"  # …and whose…
    assert "AB123" not in str(detail)  # …but the actual value never appears anywhere


# --- the wave-A guard still denies the internal surface ------------------------------


async def test_internal_case_surface_still_denied_to_external(
    b_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah, h = agent_headers(admin), agent_headers(external)
    case = await _case(make_client_case, admin, expat)
    await _assign(b_client, ah, case.id, external.id)  # even ASSIGNED…
    # …the external still cannot touch the INTERNAL routes (guard intact).
    for method, path in [
        ("GET", f"/cases/{case.id}"),
        ("GET", f"/cases/{case.id}/notes"),
        ("GET", f"/cases/{case.id}/documents"),
        ("GET", f"/cases/{case.id}/activity"),
        ("GET", "/agencies/me/members"),
        ("GET", "/cases"),
    ]:
        resp = await b_client.request(method, path, headers=h)
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code} (expected 403)"


# --- unassignment cuts access immediately --------------------------------------------


async def test_unassignment_cuts_access(
    b_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah, h = agent_headers(admin), agent_headers(external)
    case = await _case(make_client_case, admin, expat)
    await _assign(b_client, ah, case.id, external.id)
    assert (await b_client.get(f"/external/cases/{case.id}", headers=h)).status_code == 200

    removed = await b_client.delete(
        f"/cases/{case.id}/external-assignments/{external.id}", headers=ah
    )
    assert removed.status_code == 200
    # Next request → 404 (table re-read, no cache).
    assert (await b_client.get(f"/external/cases/{case.id}", headers=h)).status_code == 404


# --- assignment management borders ---------------------------------------------------


async def test_assignment_requires_agent_manage(
    b_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await _case(make_client_case, admin, expat)
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    denied = await b_client.post(
        f"/cases/{case.id}/external-assignments",
        headers=agent_headers(member),  # case.edit but NOT agent.manage
        json={"agent_id": str(external.id)},
    )
    assert denied.status_code == 403


async def test_cannot_assign_internal_agent(
    b_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case = await _case(make_client_case, admin, expat)
    internal = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    resp = await b_client.post(
        f"/cases/{case.id}/external-assignments", headers=ah, json={"agent_id": str(internal.id)}
    )
    assert resp.status_code == 422  # only an external can be assigned


# --- happy path: an assigned external can act ----------------------------------------


async def test_assigned_external_can_read_comment_upload(
    b_client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_case_note: MakeCaseNote,
    agent_headers: AuthHeaders,
) -> None:
    ah, h = agent_headers(admin), agent_headers(external)
    case = await _case(make_client_case, admin, expat)
    # A document requirement on an active step.
    tid = (await b_client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    step = (await b_client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Acte"})).json()
    await b_client.post(
        f"/journeys/{tid}/steps/{step['id']}/requirements",
        headers=ah,
        json={"kind": "document", "reference": "Acte notarié", "scope": "principal"},
    )
    steps = (
        await b_client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    pid = steps[0]["id"]
    await b_client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"}
    )
    await _assign(b_client, ah, case.id, external.id)

    detail = (await b_client.get(f"/external/cases/{case.id}", headers=h)).json()
    rid = detail["timeline"][0]["requirements"][0]["id"]

    # Upload the requirement document.
    up = await b_client.post(
        f"/external/cases/{case.id}/requirements/{rid}/document", headers=h, files={"file": PDF}
    )
    assert up.status_code == 201, up.text
    assert up.json()["is_requirement"] is True and up.json()["is_mine"] is True
    assert "uploaded_by_id" not in up.json()  # no internal UUID

    # Comment on the step thread.
    posted = await b_client.post(
        f"/external/cases/{case.id}/steps/{pid}/comments", headers=h, json={"body": "Acte joint."}
    )
    assert posted.status_code == 201
    assert posted.json()["is_mine"] is True
    # The agency sees the external's comment in the same thread.
    agent_thread = (await b_client.get(f"/cases/{case.id}/steps/{pid}/comments", headers=ah)).json()
    assert any(c["body"] == "Acte joint." for c in agent_thread)
