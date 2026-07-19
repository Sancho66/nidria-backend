"""BUG P0 (Nicolas 19/07) — L'INVARIANT de la vue générale : la liste
documents du dossier côté agence sert TOUS les documents (exigences,
dépôts libres d'étape, dossier pur, pièces de fil) — un document du
dossier est un document du dossier, une seule règle. L'investigation a
montré que le back ET le front actuels servent déjà tout : ces tests
verrouillent la règle (le repro datait du jour du deploy GAP-B — front
d'avant, même réconciliation de dates que BUG-A)."""

import io

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
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
    return await make_expat_user(email="vue-client@example.com")


def _pdf(name: str = "libre.pdf") -> dict:
    return {"file": (name, io.BytesIO(b"%PDF-1.4 libre"), "application/pdf")}


async def _case_with_step(
    client: AsyncClient, ah: dict, admin: Agent, principal: ExpatUser, make_client_case
):
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


async def test_general_view_serves_every_document_of_the_case(
    client: AsyncClient,
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """Dépôt LIBRE client sur étape + pièce de fil + dépôt dossier pur :
    la vue générale (sans filtre) les sert TOUS, avec leurs attributs."""
    ah, eh = agent_headers(admin), expat_headers(principal)
    case, pid = await _case_with_step(client, ah, admin, principal, make_client_case)
    # 1. le depot LIBRE du client sur l'etape (le repro exact)
    free = await client.post(
        f"/expat/cases/{case.id}/documents",
        headers=eh,
        files=_pdf("depot-libre-client.pdf"),
        data={"step_progress_id": pid},
    )
    assert free.status_code == 201, free.text
    # 2. une piece de FIL (agent, referencee par un message)
    thread_doc = await client.post(
        f"/cases/{case.id}/documents",
        headers=ah,
        files=_pdf("piece-fil.pdf"),
        data={"step_progress_id": pid, "kind": "deliverable"},
    )
    c = await client.post(
        f"/cases/{case.id}/steps/{pid}/comments",
        headers=ah,
        json={"body": "piece jointe", "document_id": thread_doc.json()["id"]},
    )
    assert c.status_code == 201
    # 3. un depot dossier PUR (sans etape)
    pure = await client.post(f"/cases/{case.id}/documents", headers=ah, files=_pdf("pur.pdf"))
    assert pure.status_code == 201

    generale = (await client.get(f"/cases/{case.id}/documents", headers=ah)).json()
    ids = {d["id"] for d in generale}
    assert {free.json()["id"], thread_doc.json()["id"], pure.json()["id"]} <= ids
    by_id = {d["id"]: d for d in generale}
    libre = by_id[free.json()["id"]]
    assert libre["kind"] == "deposit"  # client : force
    assert libre["uploaded_by_type"] == "expat"
    assert libre["step_name"] == "Etape"  # l'etape d'origine servie
    assert libre["is_requirement"] is False  # libre, pas une exigence


async def test_agency_deletes_a_client_deposit(
    client: AsyncClient,
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """L'agent (case.edit) supprime un dépôt CLIENT : la vue agence ET
    l'espace client le perdent proprement."""
    ah, eh = agent_headers(admin), expat_headers(principal)
    case, pid = await _case_with_step(client, ah, admin, principal, make_client_case)
    up = await client.post(
        f"/expat/cases/{case.id}/documents",
        headers=eh,
        files=_pdf(),
        data={"step_progress_id": pid},
    )
    doc_id = up.json()["id"]
    deleted = await client.delete(f"/cases/{case.id}/documents/{doc_id}", headers=ah)
    assert deleted.status_code == 200, deleted.text
    assert doc_id not in {
        d["id"] for d in (await client.get(f"/cases/{case.id}/documents", headers=ah)).json()
    }
    assert doc_id not in {
        d["id"] for d in (await client.get(f"/expat/cases/{case.id}/documents", headers=eh)).json()
    }


async def test_member_scoping_unchanged_by_general_view(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """Le cloisonnement membre est INCHANGÉ : le dépôt libre du principal
    n'apparaît pas chez le membre — il ne voit toujours que les siens."""
    ah, eh = agent_headers(admin), expat_headers(principal)
    case, pid = await _case_with_step(client, ah, admin, principal, make_client_case)
    await client.post(
        f"/expat/cases/{case.id}/documents",
        headers=eh,
        files=_pdf(),
        data={"step_progress_id": pid},
    )
    claire = await make_expat_user(email="vue-claire@example.com")
    db_session.add(
        CasePerson(case_id=case.id, kind="family", full_name="Claire", expat_user_id=claire.id)
    )
    await db_session.commit()
    member_view = (
        await client.get(f"/expat/cases/{case.id}/documents", headers=expat_headers(claire))
    ).json()
    assert member_view == []  # rien a elle, rien montre
