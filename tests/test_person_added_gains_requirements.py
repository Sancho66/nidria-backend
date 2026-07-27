"""Fin du gel de composition (ticket Nicolas 26/07, repro b).

Avant : la matérialisation lisait la composition du dossier à l'ACTIVATION de
l'étape et la gelait — un associé ajouté ensuite ne gagnait jamais ses lignes
`each_person`, même sur une étape encore active. Désormais, add_person (le
point de convergence des deux chemins d'ajout) matérialise les lignes
manquantes du nouveau venu sur chaque étape IN_PROGRESS.

Contrat conservé : TODO n'a besoin de rien (matérialise à l'activation), DONE
n'est jamais rendue incomplète (rattrape au reopen), `principal` ne vise
jamais un membre, idempotent, et le dossier voisin ne bouge pas.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.progress.progress_manager import ProgressManager
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _case_with_active_step(
    client: AsyncClient,
    admin: Agent,
    headers: dict[str, str],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    *,
    email: str,
    requirements: list[dict],
    second_step: bool = False,
) -> tuple[ClientCase, str]:
    """Un dossier avec un parcours dont l'étape 1 (porteuse des exigences)
    est ACTIVE — la matérialisation initiale a donc déjà eu lieu. L'étape 2
    optionnelle reste TODO (témoin : rien ne s'y matérialise)."""
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step = (
        await client.post(
            f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Collecte"}
        )
    ).json()
    for req in requirements:
        r = await client.post(
            f"/journeys/{template['id']}/steps/{step['id']}/requirements",
            headers=headers,
            json=req,
        )
        assert r.status_code == 201, r.text
    if second_step:
        todo = (
            await client.post(
                f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Suite"}
            )
        ).json()
        r = await client.post(
            f"/journeys/{template['id']}/steps/{todo['id']}/requirements",
            headers=headers,
            json={"kind": "document", "reference": "attestation", "scope": "each_person"},
        )
        assert r.status_code == 201, r.text
    principal = await make_expat_user(activated=True, email=email)
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    timeline = (
        await client.post(
            f"/cases/{case.id}/journey",
            headers=headers,
            json={"journey_template_id": template["id"]},
        )
    ).json()
    first = next(s for s in timeline if s["name"] == "Collecte")
    r = await client.patch(
        f"/cases/{case.id}/steps/{first['id']}", headers=headers, json={"status": "in_progress"}
    )
    assert r.status_code == 200, r.text
    return case, first["id"]


async def _rows(db: AsyncSession, person_id: str) -> list[CaseStepRequirement]:
    return list(
        (
            await db.execute(
                select(CaseStepRequirement).where(
                    CaseStepRequirement.person_id == uuid.UUID(person_id)
                )
            )
        )
        .scalars()
        .all()
    )


# --- (1) l'associé ajouté après activation gagne ses lignes, et les voit -------------


async def test_person_added_after_activation_gains_each_person_rows(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    case, active_pid = await _case_with_active_step(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email="jorge@example.com",
        requirements=[
            {"kind": "document", "reference": "passeport", "scope": "each_person"},
            {"kind": "document", "reference": "kbis", "scope": "principal"},
        ],
        second_step=True,
    )

    # L'associé arrive APRÈS l'activation — avant le fix : gel, zéro ligne.
    created = await client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Assoc Un", "relationship": "associate", "email": "assoc1@example.com"},
    )
    assert created.status_code == 201, created.text
    person_id = created.json()["id"]

    rows = await _rows(db_session, person_id)
    # SES lignes : le each_person de l'étape ACTIVE — pas le principal (kbis),
    # pas l'attestation de l'étape TODO (elle matérialisera à l'activation).
    assert [(r.kind, r.reference, r.scope, r.status) for r in rows] == [
        ("document", "passeport", "each_person", "pending")
    ]
    assert str(rows[0].case_step_progress_id) == active_pid

    # Son espace la montre — le harnais de filtrage existant : un membre ne
    # voit QUE ses exigences.
    member = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "assoc1@example.com"))
    ).scalar_one()
    member.activated_at = datetime.now(UTC)
    await db_session.commit()
    detail = (await client.get(f"/expat/cases/{case.id}", headers=expat_headers(member))).json()
    reqs = [req for step in detail["timeline"] for req in step["requirements"]]
    assert [(r["kind"], r["reference"]) for r in reqs] == [("document", "passeport")]

    # Et Jorge (principal) voit tout : ses 2 lignes + celle de l'associé.
    jorge = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "jorge@example.com"))
    ).scalar_one()
    jorge_detail = (
        await client.get(f"/expat/cases/{case.id}", headers=expat_headers(jorge))
    ).json()
    jorge_reqs = [req for step in jorge_detail["timeline"] for req in step["requirements"]]
    assert len(jorge_reqs) == 3


# --- (2) idempotence ------------------------------------------------------------------


async def test_materialization_is_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    headers = agent_headers(admin)
    case, _ = await _case_with_active_step(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email="idem@example.com",
        requirements=[{"kind": "document", "reference": "passeport", "scope": "each_person"}],
    )
    created = await client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Assoc Idem", "relationship": "associate"},
    )
    person_id = created.json()["id"]
    assert len(await _rows(db_session, person_id)) == 1

    # Double appel direct du même geste : zéro création, zéro doublon.
    person = (
        await db_session.execute(select(CasePerson).where(CasePerson.id == uuid.UUID(person_id)))
    ).scalar_one()
    case_row = (
        await db_session.execute(select(ClientCase).where(ClientCase.id == case.id))
    ).scalar_one()
    again = await ProgressManager(db_session).materialize_for_person(case_row, person)
    assert again == 0
    await db_session.commit()
    assert len(await _rows(db_session, person_id)) == 1


# --- (3) témoins : scope principal, et le dossier voisin -----------------------------


async def test_principal_scope_and_neighbor_case_untouched(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    headers = agent_headers(admin)
    # Dossier A : exigence PRINCIPAL seulement.
    case_a, _ = await _case_with_active_step(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email="only-principal@example.com",
        requirements=[{"kind": "document", "reference": "kbis", "scope": "principal"}],
    )
    # Dossier B voisin : each_person actif — l'ajout sur A ne doit rien y créer.
    case_b, _ = await _case_with_active_step(
        client,
        admin,
        headers,
        make_client_case,
        make_expat_user,
        email="neighbor@example.com",
        requirements=[{"kind": "document", "reference": "passeport", "scope": "each_person"}],
    )
    before_b = (
        await db_session.execute(
            select(CaseStepRequirement)
            .join(
                CasePerson,
                CasePerson.id == CaseStepRequirement.person_id,
            )
            .where(CasePerson.case_id == case_b.id)
        )
    ).all()

    created = await client.post(
        f"/cases/{case_a.id}/persons",
        headers=headers,
        json={"full_name": "Assoc A", "relationship": "associate"},
    )
    assert created.status_code == 201
    # Témoin scope : rien pour l'associé (l'exigence vise le principal seul).
    assert await _rows(db_session, created.json()["id"]) == []
    # Témoin cross-case : le dossier voisin n'a pas bougé d'une ligne.
    after_b = (
        await db_session.execute(
            select(CaseStepRequirement)
            .join(CasePerson, CasePerson.id == CaseStepRequirement.person_id)
            .where(CasePerson.case_id == case_b.id)
        )
    ).all()
    assert len(after_b) == len(before_b)
