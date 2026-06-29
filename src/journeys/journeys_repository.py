import uuid
from collections.abc import Collection

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.journey import (
    JourneySection,
    JourneyStepAttachment,
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateCaseField,
    JourneyTemplateField,
    JourneyTemplateStep,
    StepPrerequisite,
)
from shared.models.step_case_requirement import StepCaseRequirement
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

    async def get_templates_by_ids(
        self, template_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, JourneyTemplate]:
        """Batch-load templates by id, keyed by id. Display-only (resolving
        journey names for a page of cases without an N+1); the ids come from
        already agency-scoped cases, so no agency filter is needed."""
        ids = {tid for tid in template_ids if tid is not None}
        if not ids:
            return {}
        stmt = select(JourneyTemplate).where(JourneyTemplate.id.in_(ids))
        return {t.id: t for t in (await self.db.execute(stmt)).scalars()}

    async def list_sample_templates(self) -> list[JourneyTemplate]:
        """The shared LIBRARY samples (agency_id IS NULL + is_sample). Global,
        read-only — separate from the agency list (which excludes NULL)."""
        stmt = (
            select(JourneyTemplate)
            .where(
                JourneyTemplate.agency_id.is_(None),
                JourneyTemplate.is_sample.is_(True),
            )
            .order_by(JourneyTemplate.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_template_for_clone(
        self, agency_id: uuid.UUID, template_id: uuid.UUID
    ) -> JourneyTemplate | None:
        """A clone SOURCE: the agency's own template OR a library sample
        (agency_id NULL + is_sample). Read-only resolver — the clone itself is
        a later block. NOT a write path: never use this to mutate (that stays
        get_template_in_agency, which rejects samples)."""
        stmt = select(JourneyTemplate).where(
            JourneyTemplate.id == template_id,
            or_(
                JourneyTemplate.agency_id == agency_id,
                and_(
                    JourneyTemplate.agency_id.is_(None),
                    JourneyTemplate.is_sample.is_(True),
                ),
            ),
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

    async def count_active_cases_using_template(self, template_id: uuid.UUID) -> int:
        """Cases ACTIVELY linked to this template (deleted_at IS NULL) — the
        only ones that block a template deletion."""
        stmt = select(func.count()).where(
            ClientCase.journey_template_id == template_id,
            ClientCase.deleted_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one()

    async def count_archived_cases_using_template(self, template_id: uuid.UUID) -> int:
        """Soft-deleted cases still linked to this template — auto-detached on
        delete; surfaced to the UI so the user is warned beforehand."""
        stmt = select(func.count()).where(
            ClientCase.journey_template_id == template_id,
            ClientCase.deleted_at.is_not(None),
        )
        return (await self.db.execute(stmt)).scalar_one()

    async def detach_archived_cases_from_template(self, template_id: uuid.UUID) -> None:
        """Free the RESTRICT FKs held by ARCHIVED cases of THIS template so it
        can be deleted. STRICTLY scoped to this template: purges only the
        case_step_progress that (a) belong to a soft-deleted case linked to
        this template AND (b) reference one of THIS template's steps; then
        nulls those cases' journey_template_id. Never touches an active case,
        another template's instances, or another agency (a case linked to this
        template shares its agency). Caller commits — one atomic tx with the
        delete; a failure rolls the whole thing back."""
        archived_case_ids = (
            select(ClientCase.id)
            .where(
                ClientCase.journey_template_id == template_id,
                ClientCase.deleted_at.is_not(None),
            )
            .scalar_subquery()
        )
        template_step_ids = (
            select(JourneyTemplateStep.id)
            .where(JourneyTemplateStep.template_id == template_id)
            .scalar_subquery()
        )
        # 1) Purge the step instances (CASCADE clears their comments /
        #    participants / requirements; documents & reminders SET NULL) —
        #    these hold the case_step_progress.template_step_id RESTRICT that
        #    would otherwise block the delete.
        await self.db.execute(
            delete(CaseStepProgress).where(
                CaseStepProgress.case_id.in_(archived_case_ids),
                CaseStepProgress.template_step_id.in_(template_step_ids),
            )
        )
        # 2) Drop the client_case.journey_template_id RESTRICT link.
        await self.db.execute(
            update(ClientCase)
            .where(
                ClientCase.journey_template_id == template_id,
                ClientCase.deleted_at.is_not(None),
            )
            .values(journey_template_id=None)
        )

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
        default_validated_by_type: str,
        default_validated_by_agent_id: uuid.UUID | None = None,
    ) -> JourneyTemplateStep:
        step = JourneyTemplateStep(
            template_id=template_id,
            name=name,
            position=position,
            estimated_days=estimated_days,
            default_responsible_type=default_responsible_type,
            default_responsible_agent_id=default_responsible_agent_id,
            completion_mode=completion_mode,
            default_validated_by_type=default_validated_by_type,
            default_validated_by_agent_id=default_validated_by_agent_id,
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

    # --- step attachments (Feature 2 — descending agency content) ------------------

    async def list_step_attachments(self, step_id: uuid.UUID) -> list[JourneyStepAttachment]:
        stmt = (
            select(JourneyStepAttachment)
            .where(JourneyStepAttachment.step_id == step_id)
            .order_by(JourneyStepAttachment.position, JourneyStepAttachment.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def list_step_attachments_for_steps(
        self, step_ids: list[uuid.UUID]
    ) -> list[JourneyStepAttachment]:
        """Batched load for the template detail (no N+1)."""
        if not step_ids:
            return []
        stmt = (
            select(JourneyStepAttachment)
            .where(JourneyStepAttachment.step_id.in_(step_ids))
            .order_by(JourneyStepAttachment.position, JourneyStepAttachment.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_step_attachment_in_step(
        self, step_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> JourneyStepAttachment | None:
        stmt = select(JourneyStepAttachment).where(
            JourneyStepAttachment.id == attachment_id,
            JourneyStepAttachment.step_id == step_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def max_attachment_position(self, step_id: uuid.UUID) -> int | None:
        stmt = select(func.max(JourneyStepAttachment.position)).where(
            JourneyStepAttachment.step_id == step_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_step_attachment(self, **kwargs: object) -> JourneyStepAttachment:
        row = JourneyStepAttachment(**kwargs)
        self.db.add(row)
        return row

    # --- step participants ("Action à réaliser par", N) ----------------------------

    async def list_step_participants_for_steps(
        self, step_ids: list[uuid.UUID]
    ) -> list[JourneyStepParticipant]:
        """Batched load for the template detail (no N+1)."""
        if not step_ids:
            return []
        stmt = select(JourneyStepParticipant).where(JourneyStepParticipant.step_id.in_(step_ids))
        return list((await self.db.execute(stmt)).scalars())

    async def get_step_participant_in_step(
        self, step_id: uuid.UUID, participant_id: uuid.UUID
    ) -> JourneyStepParticipant | None:
        stmt = select(JourneyStepParticipant).where(
            JourneyStepParticipant.id == participant_id,
            JourneyStepParticipant.step_id == step_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_step_participant(self, **kwargs: object) -> JourneyStepParticipant:
        row = JourneyStepParticipant(**kwargs)
        self.db.add(row)
        return row

    async def delete_step_participant(self, row: JourneyStepParticipant) -> None:
        await self.db.delete(row)

    async def delete_step_attachment(self, row: JourneyStepAttachment) -> None:
        await self.db.delete(row)

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

    # --- step CASE requirements (vague C) — calque of step requirements ------------

    async def list_step_case_requirements(self, step_id: uuid.UUID) -> list[StepCaseRequirement]:
        stmt = (
            select(StepCaseRequirement)
            .where(StepCaseRequirement.step_id == step_id)
            .order_by(StepCaseRequirement.position, StepCaseRequirement.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_step_case_requirement_in_step(
        self, step_id: uuid.UUID, case_requirement_id: uuid.UUID
    ) -> StepCaseRequirement | None:
        stmt = select(StepCaseRequirement).where(
            StepCaseRequirement.id == case_requirement_id,
            StepCaseRequirement.step_id == step_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_step_case_requirement_by_ref(
        self, step_id: uuid.UUID, case_field: str
    ) -> StepCaseRequirement | None:
        stmt = select(StepCaseRequirement).where(
            StepCaseRequirement.step_id == step_id,
            StepCaseRequirement.case_field == case_field,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_step_case_requirement(self, **kwargs: object) -> StepCaseRequirement:
        row = StepCaseRequirement(**kwargs)
        self.db.add(row)
        return row

    async def delete_step_case_requirement(self, row: StepCaseRequirement) -> None:
        await self.db.delete(row)

    async def shift_step_case_requirement_positions(self, step_id: uuid.UUID, offset: int) -> None:
        await self.db.execute(
            update(StepCaseRequirement)
            .where(StepCaseRequirement.step_id == step_id)
            .values(position=StepCaseRequirement.position + offset)
        )

    async def set_step_case_requirement_position(
        self, case_requirement_id: uuid.UUID, position: int
    ) -> None:
        await self.db.execute(
            update(StepCaseRequirement)
            .where(StepCaseRequirement.id == case_requirement_id)
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

    # --- sections (sections chantier, vague A) -------------------------------------

    async def list_sections(self, template_id: uuid.UUID) -> list[JourneySection]:
        stmt = (
            select(JourneySection)
            .where(JourneySection.template_id == template_id)
            .order_by(JourneySection.position, JourneySection.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_section_in_template(
        self, template_id: uuid.UUID, section_id: uuid.UUID
    ) -> JourneySection | None:
        stmt = select(JourneySection).where(
            JourneySection.id == section_id,
            JourneySection.template_id == template_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def max_section_position(self, template_id: uuid.UUID) -> int | None:
        stmt = select(func.max(JourneySection.position)).where(
            JourneySection.template_id == template_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_section(
        self, template_id: uuid.UUID, name: str, description: str | None, position: int
    ) -> JourneySection:
        section = JourneySection(
            template_id=template_id, name=name, description=description, position=position
        )
        self.db.add(section)
        return section

    async def delete_section(self, section: JourneySection) -> None:
        # The FK is ON DELETE SET NULL: referencing fields (both planes)
        # fall back to the NULL bucket; their declarations survive.
        await self.db.delete(section)

    async def shift_section_positions(self, template_id: uuid.UUID, offset: int) -> None:
        await self.db.execute(
            update(JourneySection)
            .where(JourneySection.template_id == template_id)
            .values(position=JourneySection.position + offset)
        )

    async def set_section_position(self, section_id: uuid.UUID, position: int) -> None:
        await self.db.execute(
            update(JourneySection).where(JourneySection.id == section_id).values(position=position)
        )
