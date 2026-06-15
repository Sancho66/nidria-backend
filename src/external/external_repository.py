import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.case_external_assignment import CaseExternalAssignment
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.external_contact import ExternalContact


class ExternalRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- portal support ------------------------------------------------------------

    async def step_counts(self, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
        if not case_ids:
            return {}
        stmt = select(CaseStepProgress.case_id, CaseStepProgress.status).where(
            CaseStepProgress.case_id.in_(case_ids)
        )
        counts: dict[uuid.UUID, tuple[int, int]] = {}
        for case_id, status in (await self.db.execute(stmt)).all():
            done, total = counts.get(case_id, (0, 0))
            counts[case_id] = (done + (1 if status == "done" else 0), total + 1)
        return counts

    async def get_agent(self, agent_id: uuid.UUID) -> Agent | None:
        return await self.db.get(Agent, agent_id)

    async def external_contact_names(self, contact_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not contact_ids:
            return {}
        stmt = select(ExternalContact.id, ExternalContact.name).where(
            ExternalContact.id.in_(contact_ids)
        )
        return {cid: name for cid, name in (await self.db.execute(stmt)).all()}

    # --- assignment management (agency side) ---------------------------------------

    async def get_case_in_agency(
        self, agency_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        stmt = select(ClientCase).where(
            ClientCase.id == case_id,
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_external_agent_in_agency(
        self, agency_id: uuid.UUID, agent_id: uuid.UUID
    ) -> Agent | None:
        stmt = (
            select(Agent)
            .where(
                Agent.id == agent_id,
                Agent.agency_id == agency_id,
                Agent.is_external.is_(True),
            )
            .options(selectinload(Agent.role))
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def is_responsible_in_case(self, case_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
        """True iff the agent is still responsible for at least one step of
        the case — guards B-unassign so no externe stays responsible
        without dossier access (wave-C coherence)."""
        stmt = select(CaseStepProgress.id).where(
            CaseStepProgress.case_id == case_id,
            CaseStepProgress.responsible_agent_id == agent_id,
        )
        return (await self.db.execute(stmt)).first() is not None

    async def get_assignment(
        self, case_id: uuid.UUID, agent_id: uuid.UUID
    ) -> CaseExternalAssignment | None:
        stmt = select(CaseExternalAssignment).where(
            CaseExternalAssignment.case_id == case_id,
            CaseExternalAssignment.agent_id == agent_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_assignment(
        self, *, case_id: uuid.UUID, agent_id: uuid.UUID, assigned_by_agent_id: uuid.UUID
    ) -> CaseExternalAssignment:
        row = CaseExternalAssignment(
            case_id=case_id, agent_id=agent_id, assigned_by_agent_id=assigned_by_agent_id
        )
        self.db.add(row)
        return row

    async def delete_assignment(self, assignment: CaseExternalAssignment) -> None:
        await self.db.delete(assignment)

    async def list_assigned_agents(self, case_id: uuid.UUID) -> list[Agent]:
        stmt = (
            select(Agent)
            .join(CaseExternalAssignment, CaseExternalAssignment.agent_id == Agent.id)
            .where(CaseExternalAssignment.case_id == case_id)
            .options(selectinload(Agent.role))
            .order_by(Agent.last_name, Agent.first_name)
        )
        return list((await self.db.execute(stmt)).scalars())
