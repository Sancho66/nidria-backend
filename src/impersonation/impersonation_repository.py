import uuid
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.impersonation import ImpersonationLog


class ImpersonationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        # INTERNAL only: an external provider is never an impersonation
        # target (impersonation operates on internal team members).
        stmt = select(Agent).where(
            Agent.id == agent_id,
            Agent.agency_id == agency_id,
            Agent.is_external.is_(False),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_expat(self, expat_user_id: uuid.UUID) -> ExpatUser | None:
        return await self.db.get(ExpatUser, expat_user_id)

    async def expat_is_principal_in_agency(
        self, expat_user_id: uuid.UUID, agency_id: uuid.UUID
    ) -> bool:
        stmt = select(
            exists().where(
                ClientCase.principal_expat_user_id == expat_user_id,
                ClientCase.agency_id == agency_id,
            )
        )
        return bool((await self.db.execute(stmt)).scalar())

    def add_log(
        self,
        *,
        impersonator_agent_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        expires_at: datetime,
    ) -> ImpersonationLog:
        log = ImpersonationLog(
            impersonator_agent_id=impersonator_agent_id,
            target_type=target_type,
            target_id=target_id,
            expires_at=expires_at,
        )
        self.db.add(log)
        return log
