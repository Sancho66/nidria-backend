"""Agent requirement-document upload (the wave-2 gap) + the enriched
aggregated documents list (linked-vs-free classification, step name,
requirement reference) on both faces, with the expat exclusion contract
(no internal UUID)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

PDF = ("p.pdf", b"%PDF-1.4 fake", "application/pdf")


@pytest.fixture
def d_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com")


async def _step(
    client: AsyncClient, headers: dict[str, str], *, mode: str = "agency_validation"
) -> tuple[str, str]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    step = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "Collecte", "completion_mode": mode},
        )
    ).json()
    return tid, step["id"]


async def _add_doc_req(
    client: AsyncClient, headers: dict[str, str], tid: str, sid: str, ref: str
) -> dict:
    r = await client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=headers,
        json={"kind": "document", "reference": ref, "scope": "principal"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _assign_start(
    client: AsyncClient, headers: dict[str, str], case_id: str, tid: str
) -> str:
    steps = (
        await client.post(
            f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    pid = steps[0]["id"]
    await client.patch(
        f"/cases/{case_id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
    )
    return pid


def _req_status(detail: dict, pid: str, ref: str) -> dict:
    step = next(s for s in detail["progress"] if s["id"] == pid)
    return next(r for r in step["requirements"] if r["reference"] == ref)


async def _concrete_req_id(
    client: AsyncClient, headers: dict[str, str], case_id: str, pid: str, ref: str
) -> str:
    """The MATERIALIZED case_step_requirement id (not the template
    step_requirement id) — what the fulfill endpoint addresses."""
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    return _req_status(detail, pid, ref)["id"]


# --- agent requirement upload (the gap) ----------------------------------------------


async def test_agent_requirement_upload_marks_provided(
    d_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, sid = await _step(d_client, ah)
    await _add_doc_req(d_client, ah, tid, sid, "Passeport")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(d_client, ah, str(case.id), tid)
    rid = await _concrete_req_id(d_client, ah, str(case.id), pid, "Passeport")

    up = await d_client.post(
        f"/cases/{case.id}/requirements/{rid}/document", headers=ah, files={"file": PDF}
    )
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]

    detail = (await d_client.get(f"/cases/{case.id}", headers=ah)).json()
    state = _req_status(detail, pid, "Passeport")
    assert state["status"] == "provided"  # the requirement now passes provided
    assert state["document_id"] == doc_id  # linked to the uploaded file


async def test_agent_requirement_upload_auto_completes_step(
    d_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, sid = await _step(d_client, ah, mode="auto")
    await _add_doc_req(d_client, ah, tid, sid, "Passeport")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(d_client, ah, str(case.id), tid)
    rid = await _concrete_req_id(d_client, ah, str(case.id), pid, "Passeport")
    await d_client.post(
        f"/cases/{case.id}/requirements/{rid}/document", headers=ah, files={"file": PDF}
    )
    detail = (await d_client.get(f"/cases/{case.id}", headers=ah)).json()
    step = next(s for s in detail["progress"] if s["id"] == pid)
    assert step["status"] == "done"  # auto→DONE, same core as the client path
    assert step["completed_by_agent_id"] is None  # SYSTEM


async def test_agent_requirement_upload_cross_agency_404(
    d_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, sid = await _step(d_client, ah)
    await _add_doc_req(d_client, ah, tid, sid, "Passeport")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(d_client, ah, str(case.id), tid)
    rid = await _concrete_req_id(d_client, ah, str(case.id), pid, "Passeport")
    other = await make_agent(role=system_roles["admin"])  # different agency
    denied = await d_client.post(
        f"/cases/{case.id}/requirements/{rid}/document",
        headers=agent_headers(other),
        files={"file": PDF},
    )
    assert denied.status_code == 404


async def test_agent_requirement_upload_gate_case_edit(
    d_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, sid = await _step(d_client, ah)
    await _add_doc_req(d_client, ah, tid, sid, "Passeport")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(d_client, ah, str(case.id), tid)
    rid = await _concrete_req_id(d_client, ah, str(case.id), pid, "Passeport")
    viewer = await make_agent(agency_id=admin.agency_id, role=system_roles["viewer"])
    denied = await d_client.post(
        f"/cases/{case.id}/requirements/{rid}/document",
        headers=agent_headers(viewer),  # case.view only, no case.edit
        files={"file": PDF},
    )
    assert denied.status_code == 403


# --- enriched aggregated list --------------------------------------------------------


async def test_enriched_list_classifies_linked_vs_free(
    d_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, sid = await _step(d_client, ah)
    await _add_doc_req(d_client, ah, tid, sid, "Passeport")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(d_client, ah, str(case.id), tid)
    rid = await _concrete_req_id(d_client, ah, str(case.id), pid, "Passeport")

    # (1) a requirement-linked doc.
    linked = (
        await d_client.post(
            f"/cases/{case.id}/requirements/{rid}/document", headers=ah, files={"file": PDF}
        )
    ).json()
    # (2) a FREE doc attached to the step (step_progress_id set) but answering no requirement.
    free_step = (
        await d_client.post(
            f"/cases/{case.id}/documents",
            headers=ah,
            files={"file": ("free1.pdf", b"%PDF", "application/pdf")},
            data={"step_progress_id": pid},
        )
    ).json()
    # (3) a fully free doc (no step at all).
    free_loose = (
        await d_client.post(
            f"/cases/{case.id}/documents",
            headers=ah,
            files={"file": ("free2.pdf", b"%PDF", "application/pdf")},
        )
    ).json()

    by_id = {
        d["id"]: d for d in (await d_client.get(f"/cases/{case.id}/documents", headers=ah)).json()
    }
    # Linked: classified as requirement, carries the reference + step name.
    assert by_id[linked["id"]]["is_requirement"] is True
    assert by_id[linked["id"]]["requirement_reference"] == "Passeport"
    assert by_id[linked["id"]]["step_name"] == "Collecte"
    # Free-on-step: has a step name BUT is NOT a requirement (the key subtlety).
    assert by_id[free_step["id"]]["is_requirement"] is False
    assert by_id[free_step["id"]]["step_name"] == "Collecte"
    assert by_id[free_step["id"]]["requirement_reference"] is None
    # Free-loose: no step, not a requirement.
    assert by_id[free_loose["id"]]["is_requirement"] is False
    assert by_id[free_loose["id"]]["step_name"] is None


async def test_expat_list_no_internal_uuid_and_enriched(
    d_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    tid, sid = await _step(d_client, ah)
    await _add_doc_req(d_client, ah, tid, sid, "Passeport")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(d_client, ah, str(case.id), tid)
    rid = await _concrete_req_id(d_client, ah, str(case.id), pid, "Passeport")
    # Agent uploads the requirement doc; client uploads a free one.
    await d_client.post(
        f"/cases/{case.id}/requirements/{rid}/document", headers=ah, files={"file": PDF}
    )
    await d_client.post(
        f"/expat/cases/{case.id}/documents",
        headers=eh,
        files={"file": ("mine.pdf", b"%PDF", "application/pdf")},
    )

    docs = (await d_client.get(f"/expat/cases/{case.id}/documents", headers=eh)).json()
    for d in docs:
        assert "uploaded_by_id" not in d  # NO internal UUID to the client
        assert "storage_path" not in d
        assert set(d.keys()) == {
            "id",
            "case_id",
            "filename",
            "uploaded_by_type",
            "is_mine",
            "validation_status",
            "expires_at",
            "created_at",
            "step_name",
            "requirement_reference",
            "is_requirement",
        }
    by_name = {d["filename"]: d for d in docs}
    # The agent's requirement doc: linked, not mine (client view).
    assert by_name["p.pdf"]["is_requirement"] is True
    assert by_name["p.pdf"]["requirement_reference"] == "Passeport"
    assert by_name["p.pdf"]["uploaded_by_type"] == "agent"
    assert by_name["p.pdf"]["is_mine"] is False
    # The client's own free upload: mine, not a requirement.
    assert by_name["mine.pdf"]["is_mine"] is True
    assert by_name["mine.pdf"]["is_requirement"] is False
    assert pid  # silence unused
