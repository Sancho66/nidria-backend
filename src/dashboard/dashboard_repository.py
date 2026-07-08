import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Row, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplateStep
from shared.models.reminder import Reminder
from shared.models.usage import UsageEvent
from src.core.enums import (
    ActorType,
    CaseStatus,
    ReminderStatus,
    StepStatus,
    StepValidatorType,
)

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


class WorklistRepository(DashboardRepository):
    """The three extra batched reads of GET /dashboard/worklist (the
    fourth source, overdue steps, reuses my_open_steps). Same rules:
    server-side (agency_id, agent_id) filters, active cases only."""

    async def steps_to_validate(
        self, agency_id: uuid.UUID, agent_id: uuid.UUID
    ) -> Sequence[Row[Any]]:
        """Active steps awaiting an AGENCY validation that lands on MY
        desk: I am the designated validator, OR the validator is "the
        agency in general" (type agent, agent_id NULL) and I OWN the
        case (arbitrage 1 - before this, those steps surfaced in NO
        agent's queue). last_provided_at = max(provided_at) of the
        step's requirements: the v1 "waiting since" approximation."""
        provided = (
            select(
                CaseStepRequirement.case_step_progress_id.label("pid"),
                func.max(CaseStepRequirement.provided_at).label("last_provided_at"),
            )
            .group_by(CaseStepRequirement.case_step_progress_id)
            .subquery()
        )
        stmt = (
            select(
                CaseStepProgress.id,
                CaseStepProgress.case_id,
                CaseStepProgress.due_at,
                CaseStepProgress.updated_at,
                JourneyTemplateStep.name.label("step_name"),
                JourneyTemplateStep.name_i18n.label("step_name_i18n"),
                JourneyTemplateStep.estimated_days,
                ExpatUser.first_name,
                ExpatUser.last_name,
                ClientCase.dest_country,
                provided.c.last_provided_at,
            )
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
            .join(JourneyTemplateStep, JourneyTemplateStep.id == CaseStepProgress.template_step_id)
            .outerjoin(provided, provided.c.pid == CaseStepProgress.id)
            .where(
                ClientCase.agency_id == agency_id,
                ClientCase.deleted_at.is_(None),
                ClientCase.status.notin_(_TERMINAL),
                CaseStepProgress.status == StepStatus.IN_PROGRESS.value,
                CaseStepProgress.validated_by_type == StepValidatorType.AGENT.value,
                or_(
                    CaseStepProgress.validated_by_agent_id == agent_id,
                    and_(
                        CaseStepProgress.validated_by_agent_id.is_(None),
                        ClientCase.owner_agent_id == agent_id,
                    ),
                ),
            )
        )
        return (await self.db.execute(stmt)).all()

    async def documents_to_review(
        self, agency_id: uuid.UUID, agent_id: uuid.UUID
    ) -> Sequence[Row[Any]]:
        """CLIENT-uploaded documents not reviewed yet (validation_status
        NULL = never examined), on cases I own OR whose carrying step I
        am responsible for (arbitrage 2). Agency deposits never queue."""
        stmt = (
            select(
                Document.id,
                Document.case_id,
                Document.filename,
                Document.created_at,
                ExpatUser.first_name,
                ExpatUser.last_name,
                ClientCase.dest_country,
            )
            .join(ClientCase, ClientCase.id == Document.case_id)
            .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
            .outerjoin(CaseStepProgress, CaseStepProgress.id == Document.step_progress_id)
            .where(
                ClientCase.agency_id == agency_id,
                ClientCase.deleted_at.is_(None),
                ClientCase.status.notin_(_TERMINAL),
                Document.validation_status.is_(None),
                Document.uploaded_by_type == ActorType.EXPAT.value,
                or_(
                    ClientCase.owner_agent_id == agent_id,
                    CaseStepProgress.responsible_agent_id == agent_id,
                ),
            )
        )
        return (await self.db.execute(stmt)).all()

    async def reminders_to_approve(
        self, agency_id: uuid.UUID, agent_id: uuid.UUID
    ) -> Sequence[Row[Any]]:
        """to_approve reminders on the cases I OWN (arbitrage 3 - the
        reminder table carries no per-agent assignment by design)."""
        stmt = (
            select(
                Reminder.id,
                Reminder.case_id,
                Reminder.scheduled_at,
                Reminder.message_body,
                Reminder.channel,
                ExpatUser.first_name,
                ExpatUser.last_name,
                ClientCase.dest_country,
            )
            .join(ClientCase, ClientCase.id == Reminder.case_id)
            .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
            .where(
                ClientCase.agency_id == agency_id,
                ClientCase.deleted_at.is_(None),
                ClientCase.status.notin_(_TERMINAL),
                Reminder.status == ReminderStatus.TO_APPROVE.value,
                ClientCase.owner_agent_id == agent_id,
            )
        )
        return (await self.db.execute(stmt)).all()


# "Activite des clients" (bento grid): the WHITELIST of usage_event
# types, CLIENT gestures only - the agency never watches itself here.
# The three _ACTIVITY_EXPAT_GESTURES exist with agent/system actors too
# (agency deposits, agency comments, agency/auto validation): only the
# actor=expat rows qualify. Account activation is intrinsically a
# client gesture, no actor filter needed.
_ACTIVITY_EXPAT_GESTURES = ("document.added", "message.sent", "case.step_validated")
_ACTIVITY_ACTIVATION = "case.client_account_activated"


class ActivityRepository(DashboardRepository):
    async def client_activity(
        self, agency_id: uuid.UUID, since: datetime, limit: int = 15
    ) -> Sequence[Row[Any]]:
        """The AGENCY-WIDE client pulse (not just my cases), AGGREGATED
        in SQL: one row per (type, case, calendar day) with its count
        and the most recent timestamp - N deposits by the same client
        the same day collapse into one line. ONE aggregated query with
        the client join, demo and deleted cases excluded."""
        day = func.date_trunc("day", UsageEvent.created_at)
        occurred_at = func.max(UsageEvent.created_at).label("occurred_at")
        stmt = (
            select(
                UsageEvent.event_type,
                UsageEvent.case_id,
                func.count().label("count"),
                occurred_at,
                ClientCase.principal_expat_user_id,
                ExpatUser.first_name,
                ExpatUser.last_name,
            )
            .join(ClientCase, ClientCase.id == UsageEvent.case_id)
            .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
            .where(
                UsageEvent.agency_id == agency_id,
                UsageEvent.created_at >= since,
                ClientCase.is_demo.is_(False),
                ClientCase.deleted_at.is_(None),
                or_(
                    and_(
                        UsageEvent.event_type.in_(_ACTIVITY_EXPAT_GESTURES),
                        UsageEvent.actor_type == ActorType.EXPAT.value,
                    ),
                    UsageEvent.event_type == _ACTIVITY_ACTIVATION,
                ),
            )
            .group_by(
                UsageEvent.event_type,
                UsageEvent.case_id,
                day,
                ClientCase.principal_expat_user_id,
                ExpatUser.first_name,
                ExpatUser.last_name,
            )
            .order_by(occurred_at.desc())
            .limit(limit)
        )
        return (await self.db.execute(stmt)).all()
