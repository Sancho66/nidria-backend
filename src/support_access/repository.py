"""Tenant-scoped queries for the support boundary and its audit trail."""

import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.case_note import CaseNote
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplateStep, StepPrerequisite
from shared.models.rbac import Role
from shared.models.step_comment import StepComment
from shared.models.support_access import SupportAccessEvent, SupportAccessGrant


class CaseSummary(NamedTuple):
    id: uuid.UUID
    status: str


class ProgressSummary(NamedTuple):
    id: uuid.UUID
    case_id: uuid.UUID
    template_step_id: uuid.UUID
    status: str
    position: int


class SupportAccessRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def case(self, agency_id: uuid.UUID, case_id: uuid.UUID) -> ClientCase | None:
        return (
            await self.db.execute(
                select(ClientCase).where(
                    ClientCase.id == case_id,
                    ClientCase.agency_id == agency_id,
                    ClientCase.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def operators(self) -> list[Agent]:
        from src.core.rbac.baseline import PLATFORM_ROLE_NAMES

        return list(
            await self.db.scalars(
                select(Agent)
                .join(Role)
                .where(
                    Role.is_system.is_(True),
                    Role.agency_id.is_(None),
                    Role.name.in_(PLATFORM_ROLE_NAMES),
                    Agent.deactivated_at.is_(None),
                )
                .options(selectinload(Agent.role))
                .order_by(Agent.first_name, Agent.id)
            )
        )

    async def active_grant(
        self, agency_id: uuid.UUID, case_id: uuid.UUID, operator_id: uuid.UUID, now: datetime
    ) -> SupportAccessGrant | None:
        return (
            await self.db.execute(
                select(SupportAccessGrant)
                .where(
                    SupportAccessGrant.agency_id == agency_id,
                    SupportAccessGrant.case_id == case_id,
                    SupportAccessGrant.operator_id == operator_id,
                    SupportAccessGrant.revoked_at.is_(None),
                    SupportAccessGrant.expires_at > now,
                )
                .order_by(SupportAccessGrant.expires_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def grants(self, agency_id: uuid.UUID, case_id: uuid.UUID) -> list[SupportAccessGrant]:
        return list(
            await self.db.scalars(
                select(SupportAccessGrant)
                .where(
                    SupportAccessGrant.agency_id == agency_id,
                    SupportAccessGrant.case_id == case_id,
                )
                .order_by(SupportAccessGrant.created_at.desc())
                .limit(100)
            )
        )

    async def events(
        self, agency_id: uuid.UUID, case_id: uuid.UUID, page: int, page_size: int
    ) -> tuple[list[SupportAccessEvent], int]:
        criteria = (
            SupportAccessEvent.agency_id == agency_id,
            SupportAccessEvent.case_id == case_id,
        )
        total = int(
            await self.db.scalar(
                select(func.count()).select_from(SupportAccessEvent).where(*criteria)
            )
            or 0
        )
        events = list(
            await self.db.scalars(
                select(SupportAccessEvent)
                .where(*criteria)
                .order_by(SupportAccessEvent.created_at.desc(), SupportAccessEvent.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return events, total

    async def actor_names(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        rows = (
            await self.db.execute(
                select(Agent.id, Agent.first_name, Agent.last_name).where(Agent.id.in_(ids))
            )
        ).all()
        return {key: f"{first} {last}".strip() for key, first, last in rows}

    async def case_page(
        self, agency_id: uuid.UUID, page: int, page_size: int
    ) -> tuple[list[CaseSummary], int]:
        criteria = (ClientCase.agency_id == agency_id, ClientCase.deleted_at.is_(None))
        total = int(
            await self.db.scalar(select(func.count()).select_from(ClientCase).where(*criteria)) or 0
        )
        rows = (
            await self.db.execute(
                select(ClientCase.id, ClientCase.status)
                .where(*criteria)
                .order_by(ClientCase.created_at.desc(), ClientCase.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return [CaseSummary(key, status) for key, status in rows], total

    async def progress(self, case_ids: list[uuid.UUID]) -> list[ProgressSummary]:
        if not case_ids:
            return []
        rows = (
            await self.db.execute(
                select(
                    CaseStepProgress.id,
                    CaseStepProgress.case_id,
                    CaseStepProgress.template_step_id,
                    CaseStepProgress.status,
                    JourneyTemplateStep.position,
                )
                .join(
                    JourneyTemplateStep, CaseStepProgress.template_step_id == JourneyTemplateStep.id
                )
                .where(CaseStepProgress.case_id.in_(case_ids))
            )
        ).all()
        summaries = [ProgressSummary(*row) for row in rows]
        edges = (
            await self.db.execute(
                select(StepPrerequisite.step_id, StepPrerequisite.prerequisite_step_id).where(
                    StepPrerequisite.step_id.in_([row.template_step_id for row in summaries])
                )
            )
        ).all()
        done = {(row.case_id, row.template_step_id) for row in summaries if row.status == "done"}
        prerequisites: dict[uuid.UUID, set[uuid.UUID]] = {}
        for step, prerequisite in edges:
            prerequisites.setdefault(step, set()).add(prerequisite)
        return [
            row._replace(status="blocked")
            if row.status == "todo"
            and any(
                (row.case_id, required) not in done
                for required in prerequisites.get(row.template_step_id, set())
            )
            else row
            for row in summaries
        ]

    async def active_grants(
        self, agency_id: uuid.UUID, case_ids: list[uuid.UUID], operator_id: uuid.UUID, now: datetime
    ) -> dict[uuid.UUID, datetime]:
        rows = (
            await self.db.execute(
                select(SupportAccessGrant.case_id, func.max(SupportAccessGrant.expires_at))
                .where(
                    SupportAccessGrant.agency_id == agency_id,
                    SupportAccessGrant.case_id.in_(case_ids),
                    SupportAccessGrant.operator_id == operator_id,
                    SupportAccessGrant.revoked_at.is_(None),
                    SupportAccessGrant.expires_at > now,
                )
                .group_by(SupportAccessGrant.case_id)
            )
        ).all()
        return dict(rows)  # type: ignore[arg-type]

    async def people(self, case_id: uuid.UUID) -> list[tuple[CasePerson, ExpatUser | None]]:
        rows = (
            await self.db.execute(
                select(CasePerson, ExpatUser)
                .outerjoin(
                    ExpatUser,
                    CasePerson.expat_user_id == ExpatUser.id,
                )
                .where(CasePerson.case_id == case_id)
                .order_by(CasePerson.created_at)
                .limit(501)
            )
        ).all()
        return [(person, expat) for person, expat in rows]

    async def documents(self, case_id: uuid.UUID) -> list[Document]:
        return list(
            await self.db.scalars(
                select(Document)
                .where(Document.case_id == case_id)
                .order_by(Document.created_at.desc())
                .limit(501)
            )
        )

    async def notes(self, case_id: uuid.UUID) -> list[CaseNote]:
        return list(
            await self.db.scalars(
                select(CaseNote)
                .where(CaseNote.case_id == case_id)
                .order_by(CaseNote.created_at.desc())
                .limit(501)
            )
        )

    async def messages(self, case_id: uuid.UUID) -> list[StepComment]:
        return list(
            await self.db.scalars(
                select(StepComment)
                .join(
                    CaseStepProgress,
                    StepComment.case_step_progress_id == CaseStepProgress.id,
                )
                .where(CaseStepProgress.case_id == case_id, StepComment.deleted_at.is_(None))
                .order_by(StepComment.created_at.desc())
                .limit(501)
            )
        )

    def record(
        self,
        grant: SupportAccessGrant,
        actor_id: uuid.UUID,
        action: str,
        resource_id: uuid.UUID | None = None,
    ) -> None:
        self.db.add(
            SupportAccessEvent(
                agency_id=grant.agency_id,
                case_id=grant.case_id,
                grant_id=grant.id,
                actor_id=actor_id,
                action=action,
                resource_id=resource_id,
            )
        )
