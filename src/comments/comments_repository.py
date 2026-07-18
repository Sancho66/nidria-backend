import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplateStep
from shared.models.step_comment import StepComment


class CommentsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- case / step resolution (the ownership borders) ----------------------------

    async def get_case_in_agency(
        self, agency_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        stmt = select(ClientCase).where(
            ClientCase.id == case_id,
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_case_for_expat(
        self, expat_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        stmt = select(ClientCase).where(
            ClientCase.id == case_id,
            ClientCase.principal_expat_user_id == expat_id,
            ClientCase.deleted_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_progress_in_case(
        self, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> CaseStepProgress | None:
        stmt = select(CaseStepProgress).where(
            CaseStepProgress.id == progress_id, CaseStepProgress.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    # --- comments --------------------------------------------------------------------

    def add_comment(self, **kwargs: Any) -> StepComment:
        comment = StepComment(**kwargs)
        self.db.add(comment)
        return comment

    async def list_comments(self, progress_id: uuid.UUID) -> list[StepComment]:
        stmt = (
            select(StepComment)
            .where(StepComment.case_step_progress_id == progress_id)
            .order_by(StepComment.created_at, StepComment.id)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_comment(
        self, progress_id: uuid.UUID, comment_id: uuid.UUID
    ) -> StepComment | None:
        stmt = select(StepComment).where(
            StepComment.id == comment_id,
            StepComment.case_step_progress_id == progress_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    # --- author-name resolution (batch, single source) -----------------------------

    async def agent_first_names(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        stmt = select(Agent.id, Agent.first_name).where(Agent.id.in_(ids))
        return {aid: first for aid, first in (await self.db.execute(stmt)).all()}

    async def expat_labels(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        stmt = select(ExpatUser.id, ExpatUser.first_name, ExpatUser.last_name).where(
            ExpatUser.id.in_(ids)
        )
        return {
            eid: f"{first} {last}".strip()
            for eid, first, last in (await self.db.execute(stmt)).all()
        }

    # --- notification resolution -----------------------------------------------------

    async def get_agency(self, agency_id: uuid.UUID) -> Agency | None:
        return await self.db.get(Agency, agency_id)

    async def get_step_name_and_i18n(
        self, template_step_id: uuid.UUID
    ) -> tuple[str | None, dict[str, str]]:
        """(scalar name, name_i18n blob) — the blob lets the notification
        resolve the step name in the recipient's language (BLOC NOTIF-1)."""
        row = (
            await self.db.execute(
                select(JourneyTemplateStep.name, JourneyTemplateStep.name_i18n).where(
                    JourneyTemplateStep.id == template_step_id
                )
            )
        ).first()
        return (row[0], row[1]) if row is not None else (None, {})

    async def get_agent_email(self, agent_id: uuid.UUID) -> str | None:
        return (
            await self.db.execute(select(Agent.email).where(Agent.id == agent_id))
        ).scalar_one_or_none()

    async def get_principal_name_email(
        self, case: ClientCase
    ) -> tuple[str | None, str | None, str | None]:
        """(display name, email, preferred_lang). The lang feeds the client
        notification-language resolution (BLOC NOTIF-1)."""
        row = (
            await self.db.execute(
                select(
                    ExpatUser.first_name,
                    ExpatUser.last_name,
                    ExpatUser.email,
                    ExpatUser.preferred_lang,
                ).where(ExpatUser.id == case.principal_expat_user_id)
            )
        ).first()
        if row is None:
            return None, None, None
        first, last, email, lang = row
        return f"{first} {last}".strip(), email, lang

    # --- anti-burst tracker (effective-send timestamp) ------------------------------
