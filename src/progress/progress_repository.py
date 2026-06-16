import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_external_assignment import CaseExternalAssignment
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.external_contact import ExternalContact
from shared.models.journey import JourneyTemplate, JourneyTemplateStep, StepPrerequisite
from shared.models.step_case_requirement import StepCaseRequirement
from shared.models.step_comment import StepComment
from shared.models.step_requirement import StepRequirement


class ProgressRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_case_in_agency(
        self, agency_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        stmt = select(ClientCase).where(
            ClientCase.id == case_id,
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
        )
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
        stmt = select(ClientCase).where(
            ClientCase.journey_template_id == template_id,
            ClientCase.deleted_at.is_(None),
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        # INTERNAL only: responsible_type=agent must resolve to an internal
        # agent (external providers are assigned via external_contact / B).
        stmt = select(Agent).where(
            Agent.id == agent_id,
            Agent.agency_id == agency_id,
            Agent.is_external.is_(False),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_any_agent_in_agency(
        self, agency_id: uuid.UUID, agent_id: uuid.UUID
    ) -> Agent | None:
        # Wave C: a nominal step responsible may be INTERNAL or EXTERNAL —
        # this fetch does NOT filter is_external (the external case is then
        # gated by assignment_exists, not agency membership).
        stmt = select(Agent).where(Agent.id == agent_id, Agent.agency_id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def assignment_exists(self, case_id: uuid.UUID, agent_id: uuid.UUID) -> bool:
        stmt = select(CaseExternalAssignment.id).where(
            CaseExternalAssignment.case_id == case_id,
            CaseExternalAssignment.agent_id == agent_id,
        )
        return (await self.db.execute(stmt)).first() is not None

    async def ensure_external_assignment(
        self, case_id: uuid.UUID, agent_id: uuid.UUID, assigned_by_agent_id: uuid.UUID
    ) -> None:
        """Idempotent: create the case↔external link only if absent (one
        row per (case, agent), whatever the number of steps the external
        defaults on). Autoflush makes a just-added row visible to the next
        existence check within the same transaction → no duplicate."""
        if await self.assignment_exists(case_id, agent_id):
            return
        self.db.add(
            CaseExternalAssignment(
                case_id=case_id, agent_id=agent_id, assigned_by_agent_id=assigned_by_agent_id
            )
        )

    async def agents_by_ids(self, agent_ids: list[uuid.UUID]) -> dict[uuid.UUID, Agent]:
        if not agent_ids:
            return {}
        stmt = select(Agent).where(Agent.id.in_(agent_ids))
        return {a.id: a for a in (await self.db.execute(stmt)).scalars()}

    async def external_contact_names(self, contact_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not contact_ids:
            return {}
        stmt = select(ExternalContact.id, ExternalContact.name).where(
            ExternalContact.id.in_(contact_ids)
        )
        return {cid: name for cid, name in (await self.db.execute(stmt)).all()}

    async def get_external_contact_in_case(
        self, case_id: uuid.UUID, contact_id: uuid.UUID
    ) -> ExternalContact | None:
        stmt = select(ExternalContact).where(
            ExternalContact.id == contact_id, ExternalContact.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    # --- step requirements (NEW WAVE) ----------------------------------------------

    async def list_step_requirements(self, template_step_id: uuid.UUID) -> list[StepRequirement]:
        stmt = (
            select(StepRequirement)
            .where(StepRequirement.step_id == template_step_id)
            .order_by(StepRequirement.position, StepRequirement.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def list_step_case_requirements_for_steps(
        self, template_step_ids: list[uuid.UUID]
    ) -> list[StepCaseRequirement]:
        """Case-level requirement DECLARATIONS for a set of template steps
        (sections chantier, vague C). No concrete table — these are read
        and evaluated live against client_case. Empty input → []."""
        if not template_step_ids:
            return []
        stmt = (
            select(StepCaseRequirement)
            .where(StepCaseRequirement.step_id.in_(template_step_ids))
            .order_by(StepCaseRequirement.position, StepCaseRequirement.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def list_persons_for_case(self, case_id: uuid.UUID) -> list[CasePerson]:
        # Eager-load expat_user: the PRINCIPAL's display name lives there
        # (case_person.full_name is NULL for the principal), and resolving
        # it lazily in async would fail.
        stmt = (
            select(CasePerson)
            .where(CasePerson.case_id == case_id)
            .options(selectinload(CasePerson.expat_user))
        )
        return list((await self.db.execute(stmt)).scalars())

    async def count_case_requirements(self, case_step_progress_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(CaseStepRequirement)
            .where(CaseStepRequirement.case_step_progress_id == case_step_progress_id)
        )
        return (await self.db.execute(stmt)).scalar_one()

    def add_case_requirement(self, **kwargs: Any) -> CaseStepRequirement:
        row = CaseStepRequirement(**kwargs)
        self.db.add(row)
        return row

    async def started_ats(self, progress_ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
        """{progress_id: first step.started timestamp} — ONE grouped MIN
        over activity_log for the whole timeline (no per-step query / N+1).
        The key is the JSONB text `details->>'step_progress_id'`, remapped
        to UUID in Python. Robust to reopen: MIN keeps the first activation
        (reopen logs step.reopened, not step.started)."""
        if not progress_ids:
            return {}
        pid_text = ActivityLog.details["step_progress_id"].astext
        stmt = (
            select(pid_text, func.min(ActivityLog.created_at))
            .where(
                ActivityLog.action_type == "step.started",
                pid_text.in_([str(pid) for pid in progress_ids]),
            )
            .group_by(pid_text)
        )
        return {uuid.UUID(pid): started for pid, started in (await self.db.execute(stmt)).all()}

    async def comment_counts(self, progress_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        """{case_step_progress_id: non-deleted comment count} — one grouped
        COUNT for the whole timeline (no per-step query / N+1). Soft-deleted
        comments (deleted_at NOT NULL) don't inflate the badge."""
        if not progress_ids:
            return {}
        stmt = (
            select(StepComment.case_step_progress_id, func.count())
            .where(
                StepComment.case_step_progress_id.in_(progress_ids),
                StepComment.deleted_at.is_(None),
            )
            .group_by(StepComment.case_step_progress_id)
        )
        return {pid: count for pid, count in (await self.db.execute(stmt)).all()}

    async def list_case_requirements_for_progress_ids(
        self, progress_ids: list[uuid.UUID]
    ) -> list[CaseStepRequirement]:
        if not progress_ids:
            return []
        stmt = (
            select(CaseStepRequirement)
            .where(CaseStepRequirement.case_step_progress_id.in_(progress_ids))
            .order_by(CaseStepRequirement.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_step(self, step_id: uuid.UUID) -> JourneyTemplateStep | None:
        return await self.db.get(JourneyTemplateStep, step_id)

    async def get_progress_by_id(self, progress_id: uuid.UUID) -> CaseStepProgress | None:
        return await self.db.get(CaseStepProgress, progress_id)

    async def get_agency_settings_holder(self, agency_id: uuid.UUID) -> Agency | None:
        return await self.db.get(Agency, agency_id)

    async def get_owner_email(self, owner_agent_id: uuid.UUID) -> str | None:
        return (
            await self.db.execute(select(Agent.email).where(Agent.id == owner_agent_id))
        ).scalar_one_or_none()

    async def get_principal_email_and_agency_name(self, case: ClientCase) -> tuple[str | None, str]:
        email = (
            await self.db.execute(
                select(ExpatUser.email).where(ExpatUser.id == case.principal_expat_user_id)
            )
        ).scalar_one_or_none()
        name = (
            await self.db.execute(select(Agency.name).where(Agency.id == case.agency_id))
        ).scalar_one_or_none()
        return email, (name or "Votre agence")
