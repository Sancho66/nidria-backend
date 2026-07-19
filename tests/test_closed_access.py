"""BUG P1 (Nicolas, 18/07) — L'INVARIANT verrouillé côté serveur : un
contenu FOURNI reste accessible au client À VIE du compte, que l'étape
soit terminée ou le dossier clos. La clôture change la présentation,
jamais l'accès. (L'investigation a montré que le backend servait déjà
tout — ces tests l'empêchent de régresser ; la coupure vécue est front.)"""

import io

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
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
    return await make_expat_user(email="closed-client@example.com")


def _pdf(name: str = "facture.pdf") -> dict:
    return {"file": (name, io.BytesIO(b"%PDF-1.4 contenu"), "application/pdf")}


async def _build_closed_case(
    client: AsyncClient,
    db: AsyncSession,
    ah: dict[str, str],
    eh: dict[str, str],
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
) -> tuple[ClientCase, str, str, str]:
    """Étape avec exigence document REMPLIE par le client + un LIVRABLE
    agence + une discussion, puis étape DONE et dossier CLOS."""
    tid = (await client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    sid = (
        await client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Facturation"})
    ).json()["id"]
    r = await client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=ah,
        json={"kind": "document", "reference": "Facture", "scope": "principal"},
    )
    assert r.status_code == 201
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    pid = steps[0]["id"]
    started = await client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"}
    )
    assert started.status_code == 200
    detail = (await client.get(f"/expat/cases/{case.id}", headers=eh)).json()
    req_id = detail["timeline"][0]["requirements"][0]["id"]
    up_req = await client.post(
        f"/expat/cases/{case.id}/requirements/{req_id}/document", headers=eh, files=_pdf()
    )
    assert up_req.status_code == 200, up_req.text  # renvoie le detail rafraichi
    req_doc_id = up_req.json()["timeline"][0]["requirements"][0]["document_id"]
    up_del = await client.post(
        f"/cases/{case.id}/documents",
        headers=ah,
        files=_pdf("attestation.pdf"),
        data={"kind": "deliverable", "step_progress_id": pid},
    )
    assert up_del.status_code == 201
    del_doc_id = up_del.json()["id"]
    c = await client.post(
        f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "Voici votre facture."}
    )
    assert c.status_code in (200, 201)
    done = await client.patch(f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "done"})
    assert done.status_code == 200
    await db.execute(update(ClientCase).where(ClientCase.id == case.id).values(status="closed"))
    await db.commit()
    return case, pid, req_doc_id, del_doc_id


async def test_client_keeps_full_read_access_on_closed_case(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """Dossier CLOS + étape DONE : le client liste et télécharge sa facture
    ET le livrable, lit sa discussion, voit le détail de l'étape, et le
    dossier reste dans sa liste. La clôture ne coupe RIEN en lecture."""
    ah, eh = agent_headers(admin), expat_headers(principal)
    case, pid, req_doc, del_doc = await _build_closed_case(
        client, db_session, ah, eh, admin, principal, make_client_case
    )
    docs = (await client.get(f"/expat/cases/{case.id}/documents", headers=eh)).json()
    assert {d["id"] for d in docs} == {req_doc, del_doc}
    for doc_id in (req_doc, del_doc):
        dl = await client.get(f"/expat/cases/{case.id}/documents/{doc_id}/download", headers=eh)
        assert dl.status_code == 200, doc_id
    comments = (await client.get(f"/expat/cases/{case.id}/steps/{pid}/comments", headers=eh)).json()
    assert [c["body"] for c in comments] == ["Voici votre facture."]
    detail = (await client.get(f"/expat/cases/{case.id}", headers=eh)).json()
    step = detail["timeline"][0]
    assert step["status"] == "done"
    assert step["comment_count"] == 1
    assert step["requirements"][0]["document_id"] == req_doc  # la piece reste liee
    mine = (await client.get("/expat/cases", headers=eh)).json()
    assert str(case.id) in {c["id"] for c in mine}  # l'espace VIT, dossier clos compris
    # Etat actuel documente : poster sur une etape TERMINEE est PERMIS.
    post = await client.post(
        f"/expat/cases/{case.id}/steps/{pid}/comments", headers=eh, json={"body": "Merci !"}
    )
    assert post.status_code == 201


async def test_member_keeps_her_targeted_deliverable_on_closed_case(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(principal)
    case, pid, _, _ = await _build_closed_case(
        client, db_session, ah, eh, admin, principal, make_client_case
    )
    claire = await make_expat_user(email="closed-claire@example.com")
    p_claire = CasePerson(
        case_id=case.id, kind="family", full_name="Claire", expat_user_id=claire.id
    )
    db_session.add(p_claire)
    await db_session.commit()
    await db_session.refresh(p_claire)
    up = await client.post(
        f"/cases/{case.id}/documents",
        headers=ah,
        files=_pdf("traduction-claire.pdf"),
        data={"kind": "deliverable", "step_progress_id": pid, "person_id": str(p_claire.id)},
    )
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]
    ch = expat_headers(claire)
    listed = (await client.get(f"/expat/cases/{case.id}/documents", headers=ch)).json()
    assert doc_id in {d["id"] for d in listed}
    dl = await client.get(f"/expat/cases/{case.id}/documents/{doc_id}/download", headers=ch)
    assert dl.status_code == 200  # etape done + dossier clos : SA piece vit


async def test_reopen_changes_presentation_never_access(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    principal: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """La réouverture n'est plus le workaround : l'accès est IDENTIQUE
    avant/après (mêmes documents, mêmes messages) — seul le statut change."""
    ah, eh = agent_headers(admin), expat_headers(principal)
    case, pid, _, _ = await _build_closed_case(
        client, db_session, ah, eh, admin, principal, make_client_case
    )

    async def _docs() -> set:
        r = await client.get(f"/expat/cases/{case.id}/documents", headers=eh)
        return {d["id"] for d in r.json()}

    async def _comments() -> list:
        r = await client.get(f"/expat/cases/{case.id}/steps/{pid}/comments", headers=eh)
        return [c["body"] for c in r.json()]

    before_docs = await _docs()
    before_comments = await _comments()
    reopened = await client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"}
    )
    assert reopened.status_code == 200
    after_docs = await _docs()
    after_comments = await _comments()
    assert after_docs == before_docs
    assert after_comments == before_comments
