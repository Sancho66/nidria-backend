import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.external_contact import ExternalContact
from shared.models.reminder import Reminder
from src.core.enums import RecipientType, ReminderChannel, ReminderStatus


class ExpatRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_cases_for_expat(self, expat_id: uuid.UUID) -> list[tuple[ClientCase, Agency]]:
        stmt = (
            select(ClientCase, Agency)
            .join(Agency, Agency.id == ClientCase.agency_id)
            .where(
                ClientCase.principal_expat_user_id == expat_id,
                ClientCase.deleted_at.is_(None),
            )
            .order_by(ClientCase.created_at.desc(), ClientCase.id.desc())
        )
        return [(case, agency) for case, agency in (await self.db.execute(stmt)).all()]

    async def get_case_for_expat(
        self, expat_id: uuid.UUID, case_id: uuid.UUID
    ) -> tuple[ClientCase, Agency] | None:
        stmt = (
            select(ClientCase, Agency)
            .join(Agency, Agency.id == ClientCase.agency_id)
            .where(
                ClientCase.id == case_id,
                ClientCase.principal_expat_user_id == expat_id,
                ClientCase.deleted_at.is_(None),
            )
        )
        row = (await self.db.execute(stmt)).first()
        return (row[0], row[1]) if row is not None else None

    async def step_counts(self, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
        """{case_id: (done, total)} — python-side aggregation, a handful
        of rows per expat."""
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
        return {contact_id: name for contact_id, name in (await self.db.execute(stmt)).all()}

    # --- requirement fulfillment (NEW WAVE 2) --------------------------------------

    async def get_requirement_in_case(
        self, case_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> tuple[CaseStepRequirement, CaseStepProgress] | None:
        """Resolve the requirement AND its owning progress in one query,
        constrained to the case — the periphery border: a requirement of
        another case is invisible (returns None → 404)."""
        stmt = (
            select(CaseStepRequirement, CaseStepProgress)
            .join(
                CaseStepProgress,
                CaseStepProgress.id == CaseStepRequirement.case_step_progress_id,
            )
            .where(
                CaseStepRequirement.id == requirement_id,
                CaseStepProgress.case_id == case_id,
            )
        )
        row = (await self.db.execute(stmt)).first()
        return (row[0], row[1]) if row is not None else None

    async def get_case_person(self, case_id: uuid.UUID, person_id: uuid.UUID) -> CasePerson | None:
        stmt = select(CasePerson).where(CasePerson.id == person_id, CasePerson.case_id == case_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_in_app_notifications(self, case_id: uuid.UUID) -> list[Reminder]:
        stmt = (
            select(Reminder)
            .where(
                Reminder.case_id == case_id,
                Reminder.channel == ReminderChannel.IN_APP.value,
                Reminder.status == ReminderStatus.SENT.value,
                Reminder.recipient_type == RecipientType.EXPAT.value,
            )
            .order_by(Reminder.updated_at.desc())
        )
        return list((await self.db.execute(stmt)).scalars())
