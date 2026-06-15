import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.journey import (
    JourneyTemplate,
    JourneyTemplateCaseField,
    JourneyTemplateField,
    JourneyTemplateStep,
    StepPrerequisite,
)
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
        default_responsible_agent_id: uuid.UUID | None = None,
        completion_mode: str,
    ) -> JourneyTemplateStep:
        step = JourneyTemplateStep(
            template_id=template_id,
            name=name,
            position=position,
            estimated_days=estimated_days,
            default_responsible_type=default_responsible_type,
            default_responsible_agent_id=default_responsible_agent_id,
            completion_mode=completion_mode,
        )
        self.db.add(step)
        return step

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        # Any agent of the agency — INTERNAL or DURABLE EXTERNAL: an
        # external is a durable partner of the agency, so it CAN be a
        # template's default responsible (the auto-assignment at
        # instantiation keeps the wave-C invariant).
        stmt = select(Agent).where(Agent.id == agent_id, Agent.agency_id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

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

    # --- template fields (NEW WAVE) — calque of the requirement methods ------------

    async def list_fields(self, template_id: uuid.UUID) -> list[JourneyTemplateField]:
        stmt = (
            select(JourneyTemplateField)
            .where(JourneyTemplateField.template_id == template_id)
            .order_by(JourneyTemplateField.position, JourneyTemplateField.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_field_in_template(
        self, template_id: uuid.UUID, field_id: uuid.UUID
    ) -> JourneyTemplateField | None:
        stmt = select(JourneyTemplateField).where(
            JourneyTemplateField.id == field_id,
            JourneyTemplateField.template_id == template_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_field_by_reference(
        self, template_id: uuid.UUID, kind: str, reference: str
    ) -> JourneyTemplateField | None:
        stmt = select(JourneyTemplateField).where(
            JourneyTemplateField.template_id == template_id,
            JourneyTemplateField.kind == kind,
            JourneyTemplateField.reference == reference,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_field(self, **kwargs: object) -> JourneyTemplateField:
        field = JourneyTemplateField(**kwargs)
        self.db.add(field)
        return field

    async def delete_field(self, field: JourneyTemplateField) -> None:
        await self.db.delete(field)

    async def shift_field_positions(self, template_id: uuid.UUID, offset: int) -> None:
        await self.db.execute(
            update(JourneyTemplateField)
            .where(JourneyTemplateField.template_id == template_id)
            .values(position=JourneyTemplateField.position + offset)
        )

    async def set_field_position(self, field_id: uuid.UUID, position: int) -> None:
        await self.db.execute(
            update(JourneyTemplateField)
            .where(JourneyTemplateField.id == field_id)
            .values(position=position)
        )

    # --- template CASE fields (option b) — calque of the field methods -------------

    async def list_case_fields(self, template_id: uuid.UUID) -> list[JourneyTemplateCaseField]:
        stmt = (
            select(JourneyTemplateCaseField)
            .where(JourneyTemplateCaseField.template_id == template_id)
            .order_by(JourneyTemplateCaseField.position, JourneyTemplateCaseField.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_case_field_in_template(
        self, template_id: uuid.UUID, case_field_id: uuid.UUID
    ) -> JourneyTemplateCaseField | None:
        stmt = select(JourneyTemplateCaseField).where(
            JourneyTemplateCaseField.id == case_field_id,
            JourneyTemplateCaseField.template_id == template_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_case_field_by_ref(
        self, template_id: uuid.UUID, case_field: str
    ) -> JourneyTemplateCaseField | None:
        stmt = select(JourneyTemplateCaseField).where(
            JourneyTemplateCaseField.template_id == template_id,
            JourneyTemplateCaseField.case_field == case_field,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_case_field(self, **kwargs: object) -> JourneyTemplateCaseField:
        case_field = JourneyTemplateCaseField(**kwargs)
        self.db.add(case_field)
        return case_field

    async def delete_case_field(self, case_field: JourneyTemplateCaseField) -> None:
        await self.db.delete(case_field)

    async def shift_case_field_positions(self, template_id: uuid.UUID, offset: int) -> None:
        await self.db.execute(
            update(JourneyTemplateCaseField)
            .where(JourneyTemplateCaseField.template_id == template_id)
            .values(position=JourneyTemplateCaseField.position + offset)
        )

    async def set_case_field_position(self, case_field_id: uuid.UUID, position: int) -> None:
        await self.db.execute(
            update(JourneyTemplateCaseField)
            .where(JourneyTemplateCaseField.id == case_field_id)
            .values(position=position)
        )
