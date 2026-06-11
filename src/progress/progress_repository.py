import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.external_contact import ExternalContact
from shared.models.journey import JourneyTemplate, JourneyTemplateStep, StepPrerequisite


class ProgressRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_case_in_agency(
        self, agency_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        stmt = select(ClientCase).where(ClientCase.id == case_id, ClientCase.agency_id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_template_in_agency(
        self, agency_id: uuid.UUID, template_id: uuid.UUID
    ) -> JourneyTemplate | None:
        stmt = select(JourneyTemplate).where(
            JourneyTemplate.id == template_id, JourneyTemplate.agency_id == agency_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_template_steps(self, template_id: uuid.UUID) -> list[JourneyTemplateStep]:
        stmt = (
            select(JourneyTemplateStep)
            .where(JourneyTemplateStep.template_id == template_id)
            .order_by(JourneyTemplateStep.position)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_template_steps_by_ids(
        self, step_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, JourneyTemplateStep]:
        if not step_ids:
            return {}
        stmt = select(JourneyTemplateStep).where(JourneyTemplateStep.id.in_(step_ids))
        return {step.id: step for step in (await self.db.execute(stmt)).scalars()}

    async def list_prerequisites_for_steps(
        self, step_ids: list[uuid.UUID]
    ) -> list[StepPrerequisite]:
        if not step_ids:
            return []
        stmt = select(StepPrerequisite).where(StepPrerequisite.step_id.in_(step_ids))
        return list((await self.db.execute(stmt)).scalars())

    async def list_progress_for_case(self, case_id: uuid.UUID) -> list[CaseStepProgress]:
        stmt = select(CaseStepProgress).where(CaseStepProgress.case_id == case_id)
        return list((await self.db.execute(stmt)).scalars())

    async def count_progress_for_case(self, case_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(CaseStepProgress.case_id == case_id)
        return (await self.db.execute(stmt)).scalar_one()

    async def get_progress_in_case(
        self, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> CaseStepProgress | None:
        stmt = select(CaseStepProgress).where(
            CaseStepProgress.id == progress_id, CaseStepProgress.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_progress(self, **kwargs: Any) -> CaseStepProgress:
        row = CaseStepProgress(**kwargs)
        self.db.add(row)
        return row

    async def list_cases_using_template(self, template_id: uuid.UUID) -> list[ClientCase]:
        stmt = select(ClientCase).where(ClientCase.journey_template_id == template_id)
        return list((await self.db.execute(stmt)).scalars())

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        stmt = select(Agent).where(Agent.id == agent_id, Agent.agency_id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_external_contact_in_case(
        self, case_id: uuid.UUID, contact_id: uuid.UUID
    ) -> ExternalContact | None:
        stmt = select(ExternalContact).where(
            ExternalContact.id == contact_id, ExternalContact.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()
