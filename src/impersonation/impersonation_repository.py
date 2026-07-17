import uuid
from datetime import datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.impersonation import ImpersonationLog
from shared.models.rbac import Role


class ImpersonationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_an_admin_of_agency(self, agency_id: uuid.UUID) -> Agent | None:
        """An INTERNAL holder of the SYSTEM 'admin' role in the agency — the
        identity a superadmin steps in AS (full agency control, never platform
        power: the system 'admin' role excludes agency.create). Wizard- and
        seed-created agencies always have one; deterministic pick (oldest)."""
        stmt = (
            select(Agent)
            .join(Role, Role.id == Agent.role_id)
            .where(
                Agent.agency_id == agency_id,
                Agent.is_external.is_(False),
                Agent.deactivated_at.is_(None),
                Role.is_system,
                Role.name == "admin",
            )
            .order_by(Agent.created_at)
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        # INTERNAL only: an external provider is never an impersonation
        # target (impersonation operates on internal team members).
        stmt = select(Agent).where(
            Agent.id == agent_id,
            Agent.agency_id == agency_id,
            Agent.is_external.is_(False),
            Agent.deactivated_at.is_(None),  # an offboarded agent has no seat to sit in
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_expat(self, expat_user_id: uuid.UUID) -> ExpatUser | None:
        return await self.db.get(ExpatUser, expat_user_id)

    async def expat_is_impersonable_in_agency(
        self, expat_user_id: uuid.UUID, agency_id: uuid.UUID
    ) -> bool:
        """PRINCIPAL of one of the agency's cases, OR MEMBER of one
        (case_person.expat_user_id — the contributor lot gave members their
        own filtered view, so 'see as' targets EVERY person with an access).
        Still never a cross-agency master key."""
        is_principal = exists().where(
            ClientCase.principal_expat_user_id == expat_user_id,
            ClientCase.agency_id == agency_id,
        )
        is_member = exists().where(
            CasePerson.expat_user_id == expat_user_id,
            CasePerson.case_id == ClientCase.id,
            ClientCase.agency_id == agency_id,
        )
        stmt = select(or_(is_principal, is_member))
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
