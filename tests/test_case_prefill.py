"""Prefilled second dossier for an existing client (same agency).

Covers: (a) prefill copies the PERSON data (principal civil + custom
fields, family members) and NOTHING else (journey/steps/documents/tags/
status start fresh); (b) cross-agency source → 422, and the picker never
reveals another agency's dossiers (same empty answer as an unknown
email); (c) demo dossiers never appear as sources; (d) wizard-provided
fields WIN over the copy; (e) without prefill the behavior is intact."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase

pytestmark = pytest.mark.usefixtures("rbac_baseline")

EMAIL = "marie.curie@example.com"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _payload(**overrides: object) -> dict:
    return {
        "first_name": "Marie",
        "last_name": "Curie",
        "email": EMAIL,
        "origin_country": "FR",
        "dest_country": "PY",
        **overrides,
    }


async def _create_source(
    client: AsyncClient, headers: dict[str, str], *, with_journey: bool = True
) -> str:
    """First dossier: filled civil fields + custom field + a family
    member + (optionally) an assigned journey."""
    created = await client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={"key": "budget", "label": "Budget", "field_type": "text"},
    )
    assert created.status_code in (201, 409)  # idempotent across helpers
    journey_id = None
    if with_journey:
        journey = await client.post("/journeys", headers=headers, json={"name": "Résidence"})
        await client.post(
            f"/journeys/{journey.json()['id']}/steps", headers=headers, json={"name": "Dossier"}
        )
        journey_id = journey.json()["id"]
    case = await client.post(
        "/cases",
        headers=headers,
        json=_payload(
            phone="+33 1 11 11 11 11",
            nationality="Française",
            profession="Physicienne",
            custom_fields={"budget": "10k"},
            **({"journey_template_id": journey_id} if journey_id else {}),
        ),
    )
    assert case.status_code == 201, case.text
    case_id = case.json()["id"]
    member = await client.post(
        f"/cases/{case_id}/persons",
        headers=headers,
        json={
            "full_name": "Pierre Curie",
            "relationship": "spouse",
            "nationality": "Française",
            "custom_fields": {"budget": "n/a"},
        },
    )
    assert member.status_code == 201, member.text
    return case_id


async def _persons(db: AsyncSession, case_id: str) -> dict[str, CasePerson]:
    rows = (
        await db.execute(select(CasePerson).where(CasePerson.case_id == uuid.UUID(case_id)))
    ).scalars()
    return {p.kind: p for p in rows}


# --- (a) copy the persons, nothing else -----------------------------------------------------


async def test_prefill_copies_persons_and_nothing_else(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    source_id = await _create_source(client, headers)

    # The picker lists the source (journey name + date).
    picker = await client.get(f"/cases/prefill-source?email={EMAIL}", headers=headers)
    assert picker.status_code == 200, picker.text
    [candidate] = picker.json()
    assert candidate["id"] == source_id
    assert candidate["journey_name"] == "Résidence"

    created = await client.post(
        "/cases", headers=headers, json=_payload(prefill_from_case_id=source_id)
    )
    assert created.status_code == 201, created.text
    new_id = created.json()["id"]

    persons = await _persons(db_session, new_id)
    principal = persons["principal"]
    assert principal.phone == "+33 1 11 11 11 11"
    assert principal.nationality == "Française"
    assert principal.profession == "Physicienne"
    assert principal.custom_fields == {"budget": "10k"}
    family = persons["family"]
    assert family.full_name == "Pierre Curie"
    assert family.relationship == "spouse"
    assert family.nationality == "Française"
    assert family.custom_fields == {"budget": "n/a"}

    # The new dossier itself starts FRESH.
    case = await db_session.get(ClientCase, uuid.UUID(new_id))
    assert case is not None
    assert case.journey_template_id is None
    assert case.status == "prospect" and case.tags == []
    progress = (
        await db_session.execute(
            select(CaseStepProgress).where(CaseStepProgress.case_id == case.id)
        )
    ).scalars()
    assert list(progress) == []
    documents = (
        await db_session.execute(select(Document).where(Document.case_id == case.id))
    ).scalars()
    assert list(documents) == []


# --- (b) cross-agency: 422 + zero existence leak ---------------------------------------------


async def test_cross_agency_source_rejected_and_never_listed(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    source_id = await _create_source(client, agent_headers(admin), with_journey=False)

    other_admin = await make_agent(role=system_roles["admin"])
    other_headers = agent_headers(other_admin)
    # The picker of ANOTHER agency answers the SAME empty list as an
    # unknown email — no existence signal.
    known_elsewhere = await client.get(
        f"/cases/prefill-source?email={EMAIL}", headers=other_headers
    )
    unknown = await client.get(
        "/cases/prefill-source?email=nobody@example.com", headers=other_headers
    )
    assert known_elsewhere.status_code == unknown.status_code == 200
    assert known_elsewhere.json() == unknown.json() == []

    # And using the foreign id directly is a 422.
    rejected = await client.post(
        "/cases", headers=other_headers, json=_payload(prefill_from_case_id=source_id)
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "case.prefill_source_invalid"


# --- (c) demo sources excluded ---------------------------------------------------------------


async def test_demo_case_never_a_source(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    expat = await make_expat_user(email=EMAIL)
    demo = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, is_demo=True
    )
    picker = await client.get(f"/cases/prefill-source?email={EMAIL}", headers=headers)
    assert picker.json() == []
    rejected = await client.post(
        "/cases", headers=headers, json=_payload(prefill_from_case_id=str(demo.id))
    )
    assert rejected.status_code == 422


# --- (d) wizard fields win --------------------------------------------------------------------


async def test_wizard_fields_override_the_copy(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    source_id = await _create_source(client, headers, with_journey=False)

    created = await client.post(
        "/cases",
        headers=headers,
        json=_payload(
            prefill_from_case_id=source_id,
            phone="+595 999 000",
            custom_fields={"budget": "20k"},
        ),
    )
    assert created.status_code == 201, created.text
    principal = (await _persons(db_session, created.json()["id"]))["principal"]
    assert principal.phone == "+595 999 000"  # wizard wins
    assert principal.custom_fields == {"budget": "20k"}  # wizard wins
    assert principal.nationality == "Française"  # untouched fields still copied


# --- (e) without prefill: intact --------------------------------------------------------------


async def test_no_prefill_behaves_as_before(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    await _create_source(client, headers, with_journey=False)

    created = await client.post("/cases", headers=headers, json=_payload(email="fresh@example.com"))
    assert created.status_code == 201, created.text
    persons = await _persons(db_session, created.json()["id"])
    assert set(persons) == {"principal"}  # no family copied
    assert persons["principal"].phone is None
    assert persons["principal"].custom_fields == {}
