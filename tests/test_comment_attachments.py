"""Pièce jointe au fil de discussion (quick-win Nicolas, 17/07) : une
seule vérité, deux affichages — le document joint est un document GAP-B
ordinaire référencé par le message. LA DOCTRINE (arbitrage Alexandre) :
le document suit les règles documents, le fil suit les règles du fil —
le membre ciblé télécharge SA pièce sans lire le fil, c'est le cas de
Nicolas au mot près, une feature pas une fuite."""

import io

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_external_assignment import CaseExternalAssignment
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def principal(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="fil-client@example.com")


def _pdf(name: str = "piece.pdf") -> dict:
    return {"file": (name, io.BytesIO(b"%PDF-1.4 piece"), "application/pdf")}


async def _thread(
    client: AsyncClient, ah: dict, admin: Agent, principal: ExpatUser, make_client_case
) -> tuple[ClientCase, str]:
    tid = (await client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Etape"})
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    return case, steps[0]["id"]


async def test_three_faces_post_with_attachment(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    principal: ExpatUser,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """Agent, client, prestataire : chacun joint — l'upload passe par SES
    endpoints existants, le kind suit GAP-B (client=deposit force,
    externe=deliverable par defaut)."""
    ah, eh = agent_headers(admin), expat_headers(principal)
    case, pid = await _thread(client, ah, admin, principal, make_client_case)

    # AGENT (le cas de Nicolas) : il choisit deliverable
    up = await client.post(
        f"/cases/{case.id}/documents", headers=ah, files=_pdf(), data={"kind": "deliverable"}
    )
    c1 = await client.post(
        f"/cases/{case.id}/steps/{pid}/comments",
        headers=ah,
        json={
            "body": "Au nom de tel associe, merci de telecharger ce document.",
            "document_id": up.json()["id"],
        },
    )
    assert c1.status_code == 201, c1.text
    assert c1.json()["document_id"] == up.json()["id"]

    # CLIENT : deposit d'office
    up2 = await client.post(f"/expat/cases/{case.id}/documents", headers=eh, files=_pdf("recu.pdf"))
    assert up2.json()["kind"] == "deposit"
    c2 = await client.post(
        f"/expat/cases/{case.id}/steps/{pid}/comments",
        headers=eh,
        json={"body": "Voici le recu.", "document_id": up2.json()["id"]},
    )
    assert c2.status_code == 201, c2.text
    assert c2.json()["document_id"] == up2.json()["id"]

    # PRESTATAIRE : deliverable par defaut, sur son dossier assigne
    external_role = (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    provider = await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="fil-pro@pro.io"
    )
    db_session.add(
        CaseExternalAssignment(case_id=case.id, agent_id=provider.id, assigned_by_agent_id=admin.id)
    )
    await db_session.commit()
    ph = agent_headers(provider)
    up3 = await client.post(
        f"/external/cases/{case.id}/documents",
        headers=ph,
        files=_pdf("traduction.pdf"),
        data={"step_progress_id": pid},
    )
    assert up3.status_code == 201, up3.text
    c3 = await client.post(
        f"/external/cases/{case.id}/steps/{pid}/comments",
        headers=ph,
        json={"body": "Traduction livree.", "document_id": up3.json()["id"]},
    )
    assert c3.status_code == 201, c3.text
    assert c3.json()["document_id"] == up3.json()["id"]

    # UNE verite, DEUX affichages : les 3 pieces sont au panneau de l'etape
    panel = (
        await client.get(f"/cases/{case.id}/documents?step_progress_id={pid}", headers=ah)
    ).json()
    ids = {d["id"] for d in panel}
    assert {up.json()["id"], up3.json()["id"]} <= ids  # l'agent a ancre, le presta aussi
    # la piece client (upload sans etape) a ete ANCREE sur l'etape du fil
    assert up2.json()["id"] in ids


async def test_member_downloads_her_piece_without_the_thread(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """LA DOCTRINE, documentee : le document suit les regles documents, le
    fil suit les regles du fil. L'associee ciblee VOIT et TELECHARGE sa
    piece au panneau — et le fil lui reste ferme (404). Le cas de Nicolas
    au mot pres."""
    ah = agent_headers(admin)
    case, pid = await _thread(client, ah, admin, principal, make_client_case)
    associe = await make_expat_user(email="fil-associe@example.com")
    person = CasePerson(
        case_id=case.id, kind="family", full_name="Tel Associe", expat_user_id=associe.id
    )
    db_session.add(person)
    await db_session.commit()
    await db_session.refresh(person)
    up = await client.post(
        f"/cases/{case.id}/documents",
        headers=ah,
        files=_pdf("statuts.pdf"),
        data={"kind": "deliverable", "person_id": str(person.id), "step_progress_id": pid},
    )
    c = await client.post(
        f"/cases/{case.id}/steps/{pid}/comments",
        headers=ah,
        json={
            "body": "Au nom de Tel Associe, merci de telecharger ce document.",
            "document_id": up.json()["id"],
        },
    )
    assert c.status_code == 201
    mh = expat_headers(associe)
    panel = (await client.get(f"/expat/cases/{case.id}/documents", headers=mh)).json()
    assert [d["id"] for d in panel] == [up.json()["id"]]  # SA piece, au panneau
    dl = await client.get(
        f"/expat/cases/{case.id}/documents/{up.json()['id']}/download", headers=mh
    )
    assert dl.status_code == 200  # elle la telecharge
    thread = await client.get(f"/expat/cases/{case.id}/steps/{pid}/comments", headers=mh)
    assert thread.status_code == 404  # le fil, lui, reste principal-only


async def test_soft_delete_mutes_reference_never_kills_document(
    client: AsyncClient,
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(principal)
    case, pid = await _thread(client, ah, admin, principal, make_client_case)
    up = await client.post(
        f"/cases/{case.id}/documents", headers=ah, files=_pdf(), data={"step_progress_id": pid}
    )
    c = await client.post(
        f"/cases/{case.id}/steps/{pid}/comments",
        headers=ah,
        json={"body": "avec piece", "document_id": up.json()["id"]},
    )
    cid = c.json()["id"]
    d = await client.delete(f"/cases/{case.id}/steps/{pid}/comments/{cid}", headers=ah)
    assert d.status_code == 200
    thread = (await client.get(f"/cases/{case.id}/steps/{pid}/comments", headers=ah)).json()
    deleted = next(x for x in thread if x["id"] == cid)
    assert deleted["deleted"] is True
    assert deleted["document_id"] is None  # la reference est muette
    docs = (await client.get(f"/expat/cases/{case.id}/documents", headers=eh)).json()
    assert up.json()["id"] in {x["id"] for x in docs}  # le document VIT au panneau


async def test_foreign_case_document_is_404(
    client: AsyncClient,
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Un document d'un AUTRE dossier -> 404, jamais un rattachement croise."""
    ah = agent_headers(admin)
    case, pid = await _thread(client, ah, admin, principal, make_client_case)
    other = await make_client_case(agency_id=admin.agency_id)
    up_other = await client.post(f"/cases/{other.id}/documents", headers=ah, files=_pdf())
    r = await client.post(
        f"/cases/{case.id}/steps/{pid}/comments",
        headers=ah,
        json={"body": "piece etrangere", "document_id": up_other.json()["id"]},
    )
    assert r.status_code == 404
