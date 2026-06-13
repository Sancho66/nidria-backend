import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.client_case import ClientCase
from shared.models.journey import JourneyTemplate, JourneyTemplateStep, StepPrerequisite
from shared.models.step_requirement import StepRequirement


class JourneysRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- templates -------------------------------------------------------------

    async def list_templates(self, agency_id: uuid.UUID) -> list[JourneyTemplate]:
        stmt = (
            select(JourneyTemplate)
            .where(JourneyTemplate.agency_id == agency_id)
            .order_by(JourneyTemplate.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_template_in_agency(
        self, agency_id: uuid.UUID, template_id: uuid.UUID
    ) -> JourneyTemplate | None:
        stmt = select(JourneyTemplate).where(
            JourneyTemplate.id == template_id,
            JourneyTemplate.agency_id == agency_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_template(self, agency_id: uuid.UUID, name: str) -> JourneyTemplate:
        template = JourneyTemplate(agency_id=agency_id, name=name)
        self.db.add(template)
        return template

    async def delete_template(self, template: JourneyTemplate) -> None:
        await self.db.delete(template)

    async def count_cases_using_template(self, template_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(ClientCase.journey_template_id == template_id)
        return (await self.db.execute(stmt)).scalar_one()

    # --- steps -------------------------------------------------------------------

    async def list_steps(self, template_id: uuid.UUID) -> list[JourneyTemplateStep]:
        stmt = (
            select(JourneyTemplateStep)
            .where(JourneyTemplateStep.template_id == template_id)
            .order_by(JourneyTemplateStep.position)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_step_in_template(
        self, template_id: uuid.UUID, step_id: uuid.UUID
    ) -> JourneyTemplateStep | None:
        stmt = select(JourneyTemplateStep).where(
            JourneyTemplateStep.id == step_id,
            JourneyTemplateStep.template_id == template_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def max_position(self, template_id: uuid.UUID) -> int | None:
        stmt = select(func.max(JourneyTemplateStep.position)).where(
            JourneyTemplateStep.template_id == template_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_step(
        self,
        *,
        template_id: uuid.UUID,
        name: str,
        position: int,
        estimated_days: int | None,
        default_responsible_type: str | None,
        required_documents: list[str],
        completion_mode: str,
    ) -> JourneyTemplateStep:
        step = JourneyTemplateStep(
            template_id=template_id,
            name=name,
            position=position,
            estimated_days=estimated_days,
            default_responsible_type=default_responsible_type,
            required_documents=required_documents,
            completion_mode=completion_mode,
        )
        self.db.add(step)
        return step

    async def delete_step(self, step: JourneyTemplateStep) -> None:
        await self.db.delete(step)

    async def shift_positions(self, template_id: uuid.UUID, offset: int) -> None:
        await self.db.execute(
            update(JourneyTemplateStep)
            .where(JourneyTemplateStep.template_id == template_id)
            .values(position=JourneyTemplateStep.position + offset)
        )

    async def set_position(self, step_id: uuid.UUID, position: int) -> None:
        await self.db.execute(
            update(JourneyTemplateStep)
            .where(JourneyTemplateStep.id == step_id)
            .values(position=position)
        )

    # --- prerequisites ----------------------------------------------------------------

    async def list_prerequisites(self, template_id: uuid.UUID) -> list[StepPrerequisite]:
        stmt = (
            select(StepPrerequisite)
            .join(
                JourneyTemplateStep,
                JourneyTemplateStep.id == StepPrerequisite.step_id,
            )
            .where(JourneyTemplateStep.template_id == template_id)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def delete_prerequisites_of_step(self, step_id: uuid.UUID) -> None:
        await self.db.execute(delete(StepPrerequisite).where(StepPrerequisite.step_id == step_id))

    def add_prerequisite(self, step_id: uuid.UUID, prerequisite_step_id: uuid.UUID) -> None:
        self.db.add(StepPrerequisite(step_id=step_id, prerequisite_step_id=prerequisite_step_id))

    # --- step requirements (NEW WAVE) ----------------------------------------------

    async def list_requirements(self, step_id: uuid.UUID) -> list[StepRequirement]:
        stmt = (
            select(StepRequirement)
            .where(StepRequirement.step_id == step_id)
            .order_by(StepRequirement.position, StepRequirement.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_requirement_in_step(
        self, step_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> StepRequirement | None:
        stmt = select(StepRequirement).where(
            StepRequirement.id == requirement_id, StepRequirement.step_id == step_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_requirement(self, **kwargs: object) -> StepRequirement:
        requirement = StepRequirement(**kwargs)
        self.db.add(requirement)
        return requirement

    async def delete_requirement(self, requirement: StepRequirement) -> None:
        await self.db.delete(requirement)

    async def shift_requirement_positions(self, step_id: uuid.UUID, offset: int) -> None:
        await self.db.execute(
            update(StepRequirement)
            .where(StepRequirement.step_id == step_id)
            .values(position=StepRequirement.position + offset)
        )

    async def set_requirement_position(self, requirement_id: uuid.UUID, position: int) -> None:
        await self.db.execute(
            update(StepRequirement)
            .where(StepRequirement.id == requirement_id)
            .values(position=position)
        )
