import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplateStep
from src.core.enums import CaseStatus, StepStatus

# Terminal statuses excluded from the action dashboard (decision D2):
# a closed/validated case has no actions left to chase.
_TERMINAL = (CaseStatus.CLOSED.value, CaseStatus.VALIDATED.value)


class DashboardRepository:
    """Cross-case reads for the agent-centric dashboard — every query is
    filtered server-side on (agency_id, agent_id) and batched (no N+1)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def agency_default_language(self, agency_id: uuid.UUID) -> str | None:
        """The agency's default content language (i18n fallback)."""
        stmt = select(Agency.default_language).where(Agency.id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def my_open_steps(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Sequence[Row[Any]]:
        """My actionable steps across ALL active cases, in ONE join.
        A step is mine when I am its responsible (any non-done status →
        "to realize", blocked included) OR its validator AND it is active
        (in_progress → "to validate"). Done steps are excluded."""
        stmt = (
            select(
                CaseStepProgress.id,
                CaseStepProgress.case_id,
                CaseStepProgress.template_step_id,
                CaseStepProgress.status,
                CaseStepProgress.responsible_agent_id,
                CaseStepProgress.validated_by_agent_id,
                CaseStepProgress.due_at,
                JourneyTemplateStep.name.label("step_name"),
                JourneyTemplateStep.name_i18n.label("step_name_i18n"),
                JourneyTemplateStep.estimated_days,
                ExpatUser.first_name,
                ExpatUser.last_name,
                ClientCase.dest_country,
            )
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
            .join(JourneyTemplateStep, JourneyTemplateStep.id == CaseStepProgress.template_step_id)
            .where(
                ClientCase.agency_id == agency_id,
                ClientCase.deleted_at.is_(None),
                ClientCase.status.notin_(_TERMINAL),
                CaseStepProgress.status != StepStatus.DONE.value,
                or_(
                    CaseStepProgress.responsible_agent_id == agent_id,
                    and_(
                        CaseStepProgress.validated_by_agent_id == agent_id,
                        CaseStepProgress.status == StepStatus.IN_PROGRESS.value,
                    ),
                ),
            )
        )
        return (await self.db.execute(stmt)).all()

    async def done_steps_for_cases(self, case_ids: list[uuid.UUID]) -> Sequence[Row[Any]]:
        """(case_id, template_step_id) of every DONE step in the given
        cases — one query, used to compute the BLOCKED projection across
        cases (a TODO step is blocked iff a prerequisite is not in its
        case's done set)."""
        if not case_ids:
            return []
        stmt = select(CaseStepProgress.case_id, CaseStepProgress.template_step_id).where(
            CaseStepProgress.case_id.in_(case_ids),
            CaseStepProgress.status == StepStatus.DONE.value,
        )
        return (await self.db.execute(stmt)).all()

    async def my_active_cases(
        self, agency_id: uuid.UUID, agent_id: uuid.UUID
    ) -> Sequence[Row[Any]]:
        """(case_id, status) of the active cases I am involved in: I own it
        OR I am responsible/validator on at least one of its steps. One
        query (EXISTS subquery, no N+1) → my_cases count + by_status."""
        involved = (
            exists()
            .where(
                CaseStepProgress.case_id == ClientCase.id,
                or_(
                    CaseStepProgress.responsible_agent_id == agent_id,
                    CaseStepProgress.validated_by_agent_id == agent_id,
                ),
            )
            .correlate(ClientCase)
        )
        stmt = select(ClientCase.id, ClientCase.status).where(
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
            ClientCase.status.notin_(_TERMINAL),
            or_(ClientCase.owner_agent_id == agent_id, involved),
        )
        return (await self.db.execute(stmt)).all()
