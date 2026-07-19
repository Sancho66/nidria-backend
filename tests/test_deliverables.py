"""GAP-B : le livrable par dossier — cloisonnement dossier (A jamais B),
ciblage membre (la traduction de Claire visible par Claire), le
prestataire dépose sur l'étape et le client voit, le kind au contrat."""

import io

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_external_assignment import CaseExternalAssignment
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
    return await make_expat_user(email="volkov@example.com")


def _pdf() -> dict:
    return {"file": ("traduction.pdf", io.BytesIO(b"%PDF-1.4 traduction"), "application/pdf")}


async def _member(db: AsyncSession, case_id, expat: ExpatUser, name: str) -> CasePerson:
    person = CasePerson(case_id=case_id, kind="family", full_name=name, expat_user_id=expat.id)
    db.add(person)
    await db.commit()
    await db.refresh(person)
    return person


async def test_deliverable_is_case_scoped_a_never_b(
    client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    principal: ExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """Le livrable posé sur le dossier A n'apparaît JAMAIS chez B — ni
    pour le client de B, ni via le dossier B côté agence."""
    ah = agent_headers(admin)
    case_a = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    expat_b = await make_expat_user(email="autre-client@example.com")
    case_b = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat_b.id)
    up = await client.post(
        f"/cases/{case_a.id}/documents", headers=ah, files=_pdf(), data={"kind": "deliverable"}
    )
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]
    # A voit son livrable ; B (client) ne voit rien ; la liste agence de B non plus
    a_list = (
        await client.get(f"/expat/cases/{case_a.id}/documents", headers=expat_headers(principal))
    ).json()
    assert [d["id"] for d in a_list] == [doc_id]
    assert a_list[0]["kind"] == "deliverable"
    b_list = (
        await client.get(f"/expat/cases/{case_b.id}/documents", headers=expat_headers(expat_b))
    ).json()
    assert b_list == []
    b_agent = (await client.get(f"/cases/{case_b.id}/documents", headers=ah)).json()
    assert doc_id not in [d["id"] for d in b_agent]


async def test_member_targeting_claire_sees_hers(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    principal: ExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """La traduction de Claire : visible par Claire (liste ET download),
    invisible pour l'autre membre, visible du principal (il voit tout)."""
    ah = agent_headers(admin)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    claire = await make_expat_user(email="claire@example.com")
    boris = await make_expat_user(email="boris@example.com")
    p_claire = await _member(db_session, case.id, claire, "Claire Volkova")
    await _member(db_session, case.id, boris, "Boris Volkov")
    up = await client.post(
        f"/cases/{case.id}/documents",
        headers=ah,
        files=_pdf(),
        data={"kind": "deliverable", "person_id": str(p_claire.id)},
    )
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]
    assert up.json()["person_id"] == str(p_claire.id)

    claire_list = (
        await client.get(f"/expat/cases/{case.id}/documents", headers=expat_headers(claire))
    ).json()
    assert [d["id"] for d in claire_list] == [doc_id]  # SA traduction
    dl = await client.get(
        f"/expat/cases/{case.id}/documents/{doc_id}/download", headers=expat_headers(claire)
    )
    assert dl.status_code == 200  # et elle peut la telecharger
    boris_list = (
        await client.get(f"/expat/cases/{case.id}/documents", headers=expat_headers(boris))
    ).json()
    assert boris_list == []  # pas celle des autres
    principal_list = (
        await client.get(f"/expat/cases/{case.id}/documents", headers=expat_headers(principal))
    ).json()
    assert [d["id"] for d in principal_list] == [doc_id]  # le principal voit tout

    # person_id d'un AUTRE dossier -> 404, jamais un rattachement croise
    other_case = await make_client_case(agency_id=admin.agency_id)
    bad = await client.post(
        f"/cases/{other_case.id}/documents",
        headers=ah,
        files=_pdf(),
        data={"kind": "deliverable", "person_id": str(p_claire.id)},
    )
    assert bad.status_code == 404


async def test_provider_delivers_and_client_sees(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    principal: ExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """Le prestataire assigné livre sur l'étape -> le client le voit,
    kind=deliverable par défaut ; un dossier NON assigné -> 404."""
    external_role = (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    provider = await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="trad@pro.io"
    )
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    db_session.add(
        CaseExternalAssignment(case_id=case.id, agent_id=provider.id, assigned_by_agent_id=admin.id)
    )
    await db_session.commit()
    up = await client.post(
        f"/external/cases/{case.id}/documents", headers=agent_headers(provider), files=_pdf()
    )
    assert up.status_code == 201, up.text
    client_list = (
        await client.get(f"/expat/cases/{case.id}/documents", headers=expat_headers(principal))
    ).json()
    assert len(client_list) == 1
    assert client_list[0]["kind"] == "deliverable"  # le defaut du prestataire
    assert client_list[0]["uploaded_by_type"] == "agent"

    unassigned = await make_client_case(agency_id=admin.agency_id)
    denied = await client.post(
        f"/external/cases/{unassigned.id}/documents", headers=agent_headers(provider), files=_pdf()
    )
    assert denied.status_code == 404  # hors assignation : le dossier n'existe pas pour lui


async def test_kind_contract_and_expat_always_deposit(
    client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    principal: ExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """kind invalide -> 422 ; le client ne fabrique JAMAIS un livrable
    (son upload est toujours deposit) ; les deux faces servent kind."""
    ah = agent_headers(admin)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal.id)
    bad = await client.post(
        f"/cases/{case.id}/documents", headers=ah, files=_pdf(), data={"kind": "gift"}
    )
    assert bad.status_code == 422
    expat_up = await client.post(
        f"/expat/cases/{case.id}/documents", headers=expat_headers(principal), files=_pdf()
    )
    assert expat_up.status_code == 201
    assert expat_up.json()["kind"] == "deposit"  # jamais un livrable client
    agent_list = (await client.get(f"/cases/{case.id}/documents", headers=ah)).json()
    assert agent_list[0]["kind"] == "deposit"
