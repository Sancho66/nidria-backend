"""A SOFT-DELETED case must not block a guarded deletion (Eric).

A soft-deleted case (deleted_at IS NOT NULL) is invisible to the agency and
counts as "no case". Both guarded journey deletions must honour this:

  - deleting the TEMPLATE → detach-on-delete (fix #3), already correct;
  - deleting a STEP → delete_step now guards on ACTIVE cases only, then purges
    the ARCHIVED cases' dead instances of that step, then deletes.

ORDER MATTERS: guard first (a LIVE case blocks — nothing is purged), purge next
(archived only), DELETE last, all in one transaction with an IntegrityError →
clean-409 safety net.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_participant import CaseStepParticipant
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.rbac import Role
from src.journeys.journeys_repository import JourneysRepository
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep


@pytest.fixture
def journeys_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _assign(client: AsyncClient, headers: dict, case_id: uuid.UUID, tid: uuid.UUID) -> None:
    resp = await client.post(
        f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": str(tid)}
    )
    assert resp.status_code == 201, resp.text


async def _soft_delete(db: AsyncSession, case_id: uuid.UUID) -> None:
    await db.execute(
        update(ClientCase).where(ClientCase.id == case_id).values(deleted_at=datetime.now(UTC))
    )
    await db.commit()


async def _progress_count(db: AsyncSession, case_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(CaseStepProgress)
            .where(CaseStepProgress.case_id == case_id)
        )
    ).scalar_one()


async def _exists(db: AsyncSession, model: type, id_: uuid.UUID) -> bool:
    return (
        await db.execute(select(func.count()).select_from(model).where(model.id == id_))
    ).scalar_one() > 0


# --- (a) Eric's literal repro: deleting the TEMPLATE — already handled ----------------


async def test_soft_deleted_case_does_not_block_template_delete(
    journeys_client: AsyncClient,
    admin: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    headers = agent_headers(admin)
    template = await make_journey_template(agency_id=admin.agency_id)
    await make_template_step(template=template)
    case = await make_client_case(agency_id=admin.agency_id)
    await _assign(journeys_client, headers, case.id, template.id)
    await _soft_delete(db_session, case.id)

    resp = await journeys_client.delete(f"/journeys/{template.id}", headers=headers)
    assert resp.status_code == 200, resp.text  # a soft-deleted case blocks nothing


# --- (b) the fix: deleting a STEP with only an archived case → 200 --------------------


async def test_soft_deleted_case_does_not_block_step_delete(
    journeys_client: AsyncClient,
    admin: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    headers = agent_headers(admin)
    template = await make_journey_template(agency_id=admin.agency_id)
    step_a = await make_template_step(template=template)
    await make_template_step(template=template)  # a second step so A is deletable
    case = await make_client_case(agency_id=admin.agency_id)
    await _assign(journeys_client, headers, case.id, template.id)
    await _soft_delete(db_session, case.id)

    resp = await journeys_client.delete(
        f"/journeys/{template.id}/steps/{step_a.id}", headers=headers
    )
    assert resp.status_code == 200, resp.text  # the only case is archived → step deletes
    # The purge took the archived case's dead instance of step A; its instance
    # on the SECOND step survives (the case still exists, just soft-deleted).
    assert await _progress_count(db_session, case.id) == 1


# --- (c) a LIVE case blocks; the guard runs BEFORE the purge (nothing purged) ---------


async def test_active_case_blocks_step_delete_and_nothing_is_purged(
    journeys_client: AsyncClient,
    admin: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    headers = agent_headers(admin)
    template = await make_journey_template(agency_id=admin.agency_id)
    step_a = await make_template_step(template=template)
    await make_template_step(template=template)  # second step so A is deletable
    live = await make_client_case(agency_id=admin.agency_id)
    archived = await make_client_case(agency_id=admin.agency_id)
    await _assign(journeys_client, headers, live.id, template.id)
    await _assign(journeys_client, headers, archived.id, template.id)
    await _soft_delete(db_session, archived.id)
    live_before = await _progress_count(db_session, live.id)
    archived_before = await _progress_count(db_session, archived.id)

    resp = await journeys_client.delete(
        f"/journeys/{template.id}/steps/{step_a.id}", headers=headers
    )
    # The LIVE case blocks; message counts ACTIVE cases only, same wording as
    # the template guard ("N active case(s)").
    assert resp.status_code == 409, resp.text
    assert "1 active case(s)" in resp.json()["detail"]
    assert resp.json()["code"] == "journey.step_in_use"
    assert resp.json()["params"]["count"] == 1
    # The guard ran BEFORE the purge → nothing was purged, on EITHER case.
    assert await _progress_count(db_session, live.id) == live_before
    assert await _progress_count(db_session, archived.id) == archived_before


# --- (d) the purge is surgical: only the archived case, only its own instances --------


async def test_purge_takes_only_archived_case_and_spares_shared_entities(
    admin: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
    make_client_case: MakeClientCase,
    db_session: AsyncSession,
) -> None:
    """purge_archived_progress_for_step deletes ONLY soft-deleted cases'
    case_step_progress of THIS step. CASCADE takes that row's participants +
    requirements; the referenced document (SET NULL) and case_person survive; a
    LIVE case sharing the step is untouched."""
    template = await make_journey_template(agency_id=admin.agency_id)
    step = await make_template_step(template=template)

    # A LIVE case with an instance on the step (raw — this is a repo test).
    live = await make_client_case(agency_id=admin.agency_id)
    live_progress = CaseStepProgress(case_id=live.id, template_step_id=step.id)
    db_session.add(live_progress)

    # An ARCHIVED case with a RICH instance: progress + participant + a
    # requirement carrying a submitted document, keyed to the case's person.
    archived = await make_client_case(agency_id=admin.agency_id, deleted_at=datetime.now(UTC))
    person_id = (
        await db_session.execute(select(CasePerson.id).where(CasePerson.case_id == archived.id))
    ).scalar_one()
    arch_progress = CaseStepProgress(case_id=archived.id, template_step_id=step.id)
    db_session.add(arch_progress)
    await db_session.flush()
    participant = CaseStepParticipant(
        case_step_progress_id=arch_progress.id, type="expat", role="fournit_documents"
    )
    document = Document(
        case_id=archived.id,
        step_progress_id=arch_progress.id,
        filename="acte.pdf",
        storage_path="s/acte.pdf",
        uploaded_by_type="expat",
        uploaded_by_id=person_id,
    )
    db_session.add_all([participant, document])
    await db_session.flush()
    requirement = CaseStepRequirement(
        case_step_progress_id=arch_progress.id,
        person_id=person_id,
        kind="document",
        reference="acte_naissance",
        scope="principal",
        status="provided",
        document_id=document.id,
    )
    db_session.add(requirement)
    await db_session.commit()

    live_pid, arch_pid = live_progress.id, arch_progress.id
    part_id, req_id, doc_id = participant.id, requirement.id, document.id

    await JourneysRepository(db_session).purge_archived_progress_for_step(step.id)
    await db_session.commit()

    # The archived instance and its CASCADE children are gone...
    assert not await _exists(db_session, CaseStepProgress, arch_pid)
    assert not await _exists(db_session, CaseStepParticipant, part_id)
    assert not await _exists(db_session, CaseStepRequirement, req_id)
    # ...but NOTHING else: the document survives (step_progress_id SET NULL) and
    # the case_person survives (never a submitted-value casualty).
    assert await _exists(db_session, Document, doc_id)
    doc_link = (
        await db_session.execute(select(Document.step_progress_id).where(Document.id == doc_id))
    ).scalar_one()
    assert doc_link is None  # detached, not destroyed
    assert await _exists(db_session, CasePerson, person_id)
    # The LIVE case's instance on the same step is untouched.
    assert await _exists(db_session, CaseStepProgress, live_pid)
