import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.external_contact import ExternalContact
from shared.models.journey import JourneyTemplateStep
from shared.models.message_template import MessageTemplate
from shared.models.reminder import Reminder


class RemindersRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- cases / related -------------------------------------------------------

    async def get_case_in_agency(
        self, agency_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        stmt = select(ClientCase).where(
            ClientCase.id == case_id,
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_step_requirements_for_progress(
        self, progress_id: uuid.UUID
    ) -> list[CaseStepRequirement]:
        stmt = select(CaseStepRequirement).where(
            CaseStepRequirement.case_step_progress_id == progress_id
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def persons_by_id_for_case(self, case_id: uuid.UUID) -> dict[uuid.UUID, CasePerson]:
        stmt = select(CasePerson).where(CasePerson.case_id == case_id)
        return {p.id: p for p in (await self.db.execute(stmt)).scalars().all()}

    async def get_principal_display(self, case_id: uuid.UUID) -> str | None:
        stmt = (
            select(ExpatUser.first_name, ExpatUser.last_name)
            .join(ClientCase, ClientCase.principal_expat_user_id == ExpatUser.id)
            .where(ClientCase.id == case_id)
        )
        row = (await self.db.execute(stmt)).first()
        return f"{row[0]} {row[1]}" if row is not None else None

    async def get_owner_display(self, case_id: uuid.UUID) -> str | None:
        from shared.models.agent import Agent as AgentModel

        stmt = (
            select(AgentModel.first_name, AgentModel.last_name)
            .join(ClientCase, ClientCase.owner_agent_id == AgentModel.id)
            .where(ClientCase.id == case_id)
        )
        row = (await self.db.execute(stmt)).first()
        return f"{row[0]} {row[1]}" if row is not None else None

    async def get_expat(self, expat_id: uuid.UUID) -> ExpatUser | None:
        return await self.db.get(ExpatUser, expat_id)

    async def get_external_contact_in_case(
        self, case_id: uuid.UUID, contact_id: uuid.UUID
    ) -> ExternalContact | None:
        stmt = select(ExternalContact).where(
            ExternalContact.id == contact_id, ExternalContact.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_progress_in_case(
        self, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> CaseStepProgress | None:
        stmt = select(CaseStepProgress).where(
            CaseStepProgress.id == progress_id, CaseStepProgress.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_template_step(self, step_id: uuid.UUID) -> JourneyTemplateStep | None:
        return await self.db.get(JourneyTemplateStep, step_id)

    async def get_step_started_at(
        self, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> datetime | None:
        """First step.started log of the progress row — the atomic
        trace of the todo→in_progress transition."""
        stmt = select(func.min(ActivityLog.created_at)).where(
            ActivityLog.case_id == case_id,
            ActivityLog.action_type == "step.started",
            ActivityLog.details["step_progress_id"].astext == str(progress_id),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    # --- message templates --------------------------------------------------------

    async def list_message_templates(self, agency_id: uuid.UUID) -> list[MessageTemplate]:
        stmt = (
            select(MessageTemplate)
            .where(MessageTemplate.agency_id == agency_id)
            .order_by(MessageTemplate.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_message_template_in_agency(
        self, agency_id: uuid.UUID, template_id: uuid.UUID
    ) -> MessageTemplate | None:
        stmt = select(MessageTemplate).where(
            MessageTemplate.id == template_id, MessageTemplate.agency_id == agency_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_message_template(self, **kwargs: Any) -> MessageTemplate:
        template = MessageTemplate(**kwargs)
        self.db.add(template)
        return template

    async def delete_row(self, row: object) -> None:
        await self.db.delete(row)

    # --- reminders --------------------------------------------------------------------

    def add_reminder(self, **kwargs: Any) -> Reminder:
        reminder = Reminder(**kwargs)
        self.db.add(reminder)
        return reminder

    async def get_reminder_in_agency(
        self, agency_id: uuid.UUID, reminder_id: uuid.UUID
    ) -> Reminder | None:
        stmt = (
            select(Reminder)
            .join(ClientCase, ClientCase.id == Reminder.case_id)
            .where(
                Reminder.id == reminder_id,
                ClientCase.agency_id == agency_id,
                ClientCase.deleted_at.is_(None),
            )
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_reminders(
        self,
        agency_id: uuid.UUID,
        filters: dict[str, Any],
        page: int,
        page_size: int,
    ) -> tuple[list[Reminder], int]:
        stmt = (
            select(Reminder)
            .join(ClientCase, ClientCase.id == Reminder.case_id)
            .where(ClientCase.agency_id == agency_id, ClientCase.deleted_at.is_(None))
        )
        if filters.get("status"):
            stmt = stmt.where(Reminder.status.in_([s.value for s in filters["status"]]))
        if filters.get("case_id"):
            stmt = stmt.where(Reminder.case_id == filters["case_id"])
        if filters.get("scheduled_from"):
            stmt = stmt.where(Reminder.scheduled_at >= filters["scheduled_from"])
        if filters.get("scheduled_to"):
            stmt = stmt.where(Reminder.scheduled_at <= filters["scheduled_to"])
        total = (
            await self.db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        stmt = (
            stmt.order_by(Reminder.scheduled_at, Reminder.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list((await self.db.execute(stmt)).scalars()), total
