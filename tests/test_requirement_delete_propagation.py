"""BUG (client reel — Domiciliation Bulgarie, dossier "Christophe") +
son correctif.

Une "information a collecter" supprimee au niveau du parcours-type restait
affichee dans l'etape, en vue dossier > parcours, quand elle avait deja ete
instanciee/soumise cote client : `step_requirement` (template) etait
hard-delete, mais la FK `case_step_requirement.step_requirement_id` est ON
DELETE SET NULL et `kind`/`reference` snapshottes -> la ligne d'instance
SURVIVAIT, et la timeline (`GET /cases/{id}/steps`) projette l'instance.

Correctif : la suppression PROPAGE explicitement (manager, meme transaction)
le retrait des instances ; les valeurs soumises ne sont JAMAIS detruites
(champ sur case_person, document dans `document`) ; un compteur pre-delete
expose l'impact. La FK SET NULL devient un DETECTEUR de fuite : plus aucun
orphelin ne doit apparaitre du fait d'une suppression.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.progress.progress_manager import ProgressManager
from src.progress.progress_repository import ProgressRepository
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCasePerson, MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(
        email="christophe@example.com", first_name="Christophe", last_name="Bulgarie"
    )


@pytest_asyncio.fixture
async def external_role(db_session: AsyncSession) -> Role:
    return (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()


@pytest_asyncio.fixture
async def external(make_agent: MakeAgent, admin: Agent, external_role: Role) -> Agent:
    return await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="lawyer@a.io"
    )


# --- helpers -------------------------------------------------------------------------


async def _field_template(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    scope: str,
    reference: str = "passport_number",
    name: str = "Domiciliation Bulgarie",
) -> tuple[str, str, dict]:
    """Template + one step + one base_field info-to-collect. Returns
    (template_id, step_id, template_requirement)."""
    tid = (await client.post("/journeys", headers=headers, json={"name": name})).json()["id"]
    sid = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "Collecte", "completion_mode": "agency_validation"},
        )
    ).json()["id"]
    await client.post(
        f"/journeys/{tid}/fields",
        headers=headers,
        json={"kind": "base_field", "reference": reference},
    )
    req = (
        await client.post(
            f"/journeys/{tid}/steps/{sid}/requirements",
            headers=headers,
            json={"kind": "base_field", "reference": reference, "scope": scope},
        )
    ).json()
    return tid, sid, req


async def _assign_start(
    client: AsyncClient, headers: dict[str, str], case_id: str, tid: str
) -> str:
    steps = (
        await client.post(
            f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    pid = steps[0]["id"]
    started = await client.patch(
        f"/cases/{case_id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
    )
    assert started.status_code == 200, started.text
    return pid


async def _expat_instances(
    client: AsyncClient, expat_headers: AuthHeaders, expat: ExpatUser, case_id: str, reference: str
) -> list[dict]:
    detail = (await client.get(f"/expat/cases/{case_id}", headers=expat_headers(expat))).json()
    return [
        r for st in detail["timeline"] for r in st["requirements"] if r["reference"] == reference
    ]


async def _answer(
    client: AsyncClient,
    expat_headers: AuthHeaders,
    expat: ExpatUser,
    case_id: str,
    instance_id: str,
    value: str = "AB123456",
) -> None:
    put = await client.put(
        f"/expat/cases/{case_id}/requirements/{instance_id}",
        headers=expat_headers(expat),
        json={"value": value},
    )
    assert put.status_code == 200, put.text


async def _agent_refs(client: AsyncClient, headers: dict[str, str], case_id: str) -> list[str]:
    steps = (await client.get(f"/cases/{case_id}/steps", headers=headers)).json()
    return [r["reference"] for s in steps for r in s["requirements"]]


# --- 1. the reported bug: deleting the info removes it from the case view -------------


async def test_deleting_a_template_requirement_removes_it_from_the_case_view(
    client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid, template_req = await _field_template(client, headers, scope="principal")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(client, headers, str(case.id), tid)
    inst = (await _expat_instances(client, expat_headers, expat, str(case.id), "passport_number"))[
        0
    ]
    await _answer(client, expat_headers, expat, str(case.id), inst["id"])

    # Precondition: shown AND provided in the case > journey view.
    before = (await client.get(f"/cases/{case.id}/steps", headers=headers)).json()
    shown = [r for s in before for r in s["requirements"] if r["reference"] == "passport_number"]
    assert shown and shown[0]["status"] == "provided" and shown[0]["value"] == "AB123456"

    deleted = await client.delete(
        f"/journeys/{tid}/steps/{sid}/requirements/{template_req['id']}", headers=headers
    )
    assert deleted.status_code == 200, deleted.text

    assert "passport_number" not in await _agent_refs(client, headers, str(case.id))


# --- 2. zero orphan: the FK is now a leak detector, never the mechanism ---------------


async def test_no_orphan_instance_after_deletion(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid, template_req = await _field_template(client, headers, scope="principal")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(client, headers, str(case.id), tid)
    inst = (await _expat_instances(client, expat_headers, expat, str(case.id), "passport_number"))[
        0
    ]
    await _answer(client, expat_headers, expat, str(case.id), inst["id"])

    await client.delete(
        f"/journeys/{tid}/steps/{sid}/requirements/{template_req['id']}", headers=headers
    )

    # No row was orphaned (SET NULL) — the instances were removed outright.
    orphans = (
        await db_session.execute(
            select(func.count())
            .select_from(CaseStepRequirement)
            .where(CaseStepRequirement.step_requirement_id.is_(None))
        )
    ).scalar_one()
    assert orphans == 0
    # And the instance for this definition is gone (no leftover row at all).
    remaining = (
        await db_session.execute(select(func.count()).select_from(CaseStepRequirement))
    ).scalar_one()
    assert remaining == 0


# --- 3. the CENTRAL invariant: submitted data survives -------------------------------


async def test_submitted_field_value_survives_deletion(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid, template_req = await _field_template(client, headers, scope="principal")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(client, headers, str(case.id), tid)
    inst = (await _expat_instances(client, expat_headers, expat, str(case.id), "passport_number"))[
        0
    ]
    await _answer(client, expat_headers, expat, str(case.id), inst["id"])

    await client.delete(
        f"/journeys/{tid}/steps/{sid}/requirements/{template_req['id']}", headers=headers
    )

    # The value the client submitted still lives on case_person — untouched.
    survived = (
        await db_session.execute(
            select(func.count())
            .select_from(CasePerson)
            .where(CasePerson.case_id == case.id, CasePerson.passport_number == "AB123456")
        )
    ).scalar_one()
    assert survived == 1


async def test_uploaded_document_survives_deletion(
    client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    sid = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "Collecte", "completion_mode": "agency_validation"},
        )
    ).json()["id"]
    template_req = (
        await client.post(
            f"/journeys/{tid}/steps/{sid}/requirements",
            headers=headers,
            json={"kind": "document", "reference": "Passeport", "scope": "principal"},
        )
    ).json()
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(client, headers, str(case.id), tid)
    inst = (await _expat_instances(client, expat_headers, expat, str(case.id), "Passeport"))[0]
    up = await client.post(
        f"/expat/cases/{case.id}/requirements/{inst['id']}/document",
        headers=expat_headers(expat),
        files={"file": ("passport.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert up.status_code == 200, up.text

    await client.delete(
        f"/journeys/{tid}/steps/{sid}/requirements/{template_req['id']}", headers=headers
    )

    # The requirement line is gone, but the uploaded document survives.
    assert "Passeport" not in await _agent_refs(client, headers, str(case.id))
    docs = (await client.get(f"/cases/{case.id}/documents", headers=headers)).json()
    assert any(d["filename"] == "passport.pdf" for d in docs)


# --- 4. the three faces stop showing the requirement ---------------------------------


async def test_all_three_faces_stop_showing_the_requirement(
    client: AsyncClient,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid, template_req = await _field_template(client, headers, scope="principal")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(client, headers, str(case.id), tid)
    inst = (await _expat_instances(client, expat_headers, expat, str(case.id), "passport_number"))[
        0
    ]
    await _answer(client, expat_headers, expat, str(case.id), inst["id"])

    # Provider assigned then named responsible for the step (invariant order).
    await client.post(
        f"/cases/{case.id}/external-assignments",
        headers=headers,
        json={"agent_id": str(external.id)},
    )
    await client.put(
        f"/cases/{case.id}/steps/{pid}/responsible",
        headers=headers,
        json={"responsible_type": "agent", "responsible_agent_id": str(external.id)},
    )

    async def agent_refs() -> list[str]:
        return await _agent_refs(client, headers, str(case.id))

    async def expat_refs() -> list[str]:
        d = (await client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))).json()
        return [r["reference"] for s in d["timeline"] for r in s["requirements"]]

    async def external_refs() -> list[str]:
        d = (await client.get(f"/external/cases/{case.id}", headers=agent_headers(external))).json()
        return [r["reference"] for s in d["timeline"] for r in s["requirements"]]

    # Precondition: all three faces show it.
    assert "passport_number" in await agent_refs()
    assert "passport_number" in await expat_refs()
    assert "passport_number" in await external_refs()

    await client.delete(
        f"/journeys/{tid}/steps/{sid}/requirements/{template_req['id']}", headers=headers
    )

    assert "passport_number" not in await agent_refs()
    assert "passport_number" not in await expat_refs()
    assert "passport_number" not in await external_refs()


# --- 5. the counter is exact on a 3-case set (one case with 2 persons) ----------------


async def test_impact_counter_is_exact(
    client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_case_person: MakeCasePerson,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid, template_req = await _field_template(client, headers, scope="each_person")

    async def _case() -> str:
        c = await make_client_case(
            agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
        )
        return str(c.id)

    # Case 1: principal answered.
    c1 = await _case()
    await _assign_start(client, headers, c1, tid)
    i1 = (await _expat_instances(client, expat_headers, expat, c1, "passport_number"))[0]
    await _answer(client, expat_headers, expat, c1, i1["id"])

    # Case 2: principal NOT answered.
    c2 = await _case()
    await _assign_start(client, headers, c2, tid)

    # Case 3: TWO persons — principal answered, family NOT.
    c3_case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await make_case_person(case=c3_case, full_name="Enfant Bulgarie")
    c3 = str(c3_case.id)
    await _assign_start(client, headers, c3, tid)
    insts3 = await _expat_instances(client, expat_headers, expat, c3, "passport_number")
    principal3 = next(r for r in insts3 if r["person_label"] == "Christophe Bulgarie")
    await _answer(client, expat_headers, expat, c3, principal3["id"])

    impact = await client.get(
        f"/journeys/{tid}/steps/{sid}/requirements/{template_req['id']}/impact", headers=headers
    )
    assert impact.status_code == 200, impact.text
    # 3 cases carry it; 2 distinct cases have a response (c1, c3); 2 provided
    # instances (c1 principal, c3 principal — c3's family stays pending).
    assert impact.json() == {
        "cases_with_response": 2,
        "responses_count": 2,
        "cases_total": 3,
    }


# --- the counter reads in ONE query, no N+1 whatever the case count -------------------


async def test_impact_query_count_is_one(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid, template_req = await _field_template(client, headers, scope="principal")
    for _ in range(3):
        case = await make_client_case(
            agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
        )
        await _assign_start(client, headers, str(case.id), tid)

    engine = db_session.get_bind()
    counter = {"n": 0}

    def _count(*_a: object, **_k: object) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        rows = await ProgressRepository(db_session).requirement_instances_for_definition(
            uuid.UUID(template_req["id"])
        )
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert len(rows) == 3  # one instance per case
    assert counter["n"] == 1  # a single batched query, no N+1


# --- 6. isolation: deleting agency A's requirement never touches agency B --------------


async def test_deletion_is_isolated_across_agencies(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers_a = agent_headers(admin)
    tid_a, sid_a, req_a = await _field_template(client, headers_a, scope="principal")
    case_a = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(client, headers_a, str(case_a.id), tid_a)
    ia = (await _expat_instances(client, expat_headers, expat, str(case_a.id), "passport_number"))[
        0
    ]
    await _answer(client, expat_headers, expat, str(case_a.id), ia["id"])

    agency_b = await make_agency(name="Agence B")
    admin_b = await make_agent(
        agency_id=agency_b.id, role=system_roles["admin"], email="adminb@b.io"
    )
    headers_b = agent_headers(admin_b)
    tid_b, sid_b, _req_b = await _field_template(client, headers_b, scope="principal", name="B")
    case_b = await make_client_case(
        agency_id=agency_b.id, principal_expat_user_id=expat.id, owner_agent_id=admin_b.id
    )
    await _assign_start(client, headers_b, str(case_b.id), tid_b)
    ib = (await _expat_instances(client, expat_headers, expat, str(case_b.id), "passport_number"))[
        0
    ]
    await _answer(client, expat_headers, expat, str(case_b.id), ib["id"])

    # Delete agency A's requirement.
    await client.delete(
        f"/journeys/{tid_a}/steps/{sid_a}/requirements/{req_a['id']}", headers=headers_a
    )

    # A no longer shows it; B is fully untouched (still shown, value intact).
    assert "passport_number" not in await _agent_refs(client, headers_a, str(case_a.id))
    assert "passport_number" in await _agent_refs(client, headers_b, str(case_b.id))
    b_instances = (
        await db_session.execute(
            select(func.count())
            .select_from(CaseStepRequirement)
            .join(
                CaseStepProgress,
                CaseStepProgress.id == CaseStepRequirement.case_step_progress_id,
            )
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .where(ClientCase.agency_id == agency_b.id)
        )
    ).scalar_one()
    assert b_instances == 1  # B's instance survived A's deletion


# --- defense in depth: an orphan instance is never displayed --------------------------


async def test_orphan_instance_is_hidden_on_all_three_faces(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """A `case_step_requirement` with step_requirement_id IS NULL (a legacy
    orphan, or a future propagation leak) must never surface — on agent,
    expat OR provider. A normal sibling requirement stays visible."""
    headers = agent_headers(admin)
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    sid = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "Collecte", "completion_mode": "agency_validation"},
        )
    ).json()["id"]
    for ref in ("passport_number", "nationality"):
        await client.post(
            f"/journeys/{tid}/fields",
            headers=headers,
            json={"kind": "base_field", "reference": ref},
        )
        await client.post(
            f"/journeys/{tid}/steps/{sid}/requirements",
            headers=headers,
            json={"kind": "base_field", "reference": ref, "scope": "principal"},
        )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(client, headers, str(case.id), tid)
    await client.post(
        f"/cases/{case.id}/external-assignments",
        headers=headers,
        json={"agent_id": str(external.id)},
    )
    await client.put(
        f"/cases/{case.id}/steps/{pid}/responsible",
        headers=headers,
        json={"responsible_type": "agent", "responsible_agent_id": str(external.id)},
    )

    # Orphan the passport_number instance (simulate the legacy behaviour).
    await db_session.execute(
        update(CaseStepRequirement)
        .where(
            CaseStepRequirement.reference == "passport_number",
            CaseStepRequirement.step_requirement_id.is_not(None),
        )
        .values(step_requirement_id=None)
    )
    await db_session.commit()

    async def expat_refs() -> list[str]:
        d = (await client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))).json()
        return [r["reference"] for s in d["timeline"] for r in s["requirements"]]

    async def external_refs() -> list[str]:
        d = (await client.get(f"/external/cases/{case.id}", headers=agent_headers(external))).json()
        return [r["reference"] for s in d["timeline"] for r in s["requirements"]]

    for refs in (
        await _agent_refs(client, headers, str(case.id)),
        await expat_refs(),
        await external_refs(),
    ):
        assert "passport_number" not in refs  # the orphan is hidden
        assert "nationality" in refs  # the normal requirement still shows


async def test_sync_missing_requirements_does_not_rematerialize_an_orphan(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """THE test that protects the nuance: the display filter lives at the
    projection, NOT in list_case_requirements_for_progress_ids. That query
    feeds _sync_missing_requirements' dedup — if it stopped returning the
    orphan, the still-present definition would be re-materialized and
    collide on uq_case_step_requirement. Here the orphan stays seen, so
    the diff-materialization creates nothing."""
    headers = agent_headers(admin)
    tid, sid, _req = await _field_template(client, headers, scope="principal")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(client, headers, str(case.id), tid)

    # Orphan the instance while its template definition still exists.
    await db_session.execute(
        update(CaseStepRequirement)
        .where(CaseStepRequirement.reference == "passport_number")
        .values(step_requirement_id=None)
    )
    await db_session.commit()

    progress = (
        await db_session.execute(
            select(CaseStepProgress).where(CaseStepProgress.id == uuid.UUID(pid))
        )
    ).scalar_one()
    created = await ProgressManager(db_session)._sync_missing_requirements(progress)
    await db_session.flush()  # would raise IntegrityError here if it collided

    assert created == 0  # nothing re-materialized (the orphan is deduped)
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(CaseStepRequirement)
            .where(CaseStepRequirement.reference == "passport_number")
        )
    ).scalar_one()
    assert count == 1  # still the single orphaned row — no duplicate, no collision
