import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.invitation import AgentInvitation
from shared.models.rbac import Role
from src.core.enums import InvitationStatus


class AgenciesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_agency(self, agency_id: uuid.UUID) -> Agency | None:
        return await self.db.get(Agency, agency_id)

    async def get_role(self, role_id: uuid.UUID) -> Role | None:
        return await self.db.get(Role, role_id)

    async def list_agents_with_roles(self, agency_id: uuid.UUID) -> list[Agent]:
        # INTERNAL agents only — externals must never appear as candidate
        # owners / responsibles in the member selector.
        stmt = (
            select(Agent)
            .options(selectinload(Agent.role))
            .where(Agent.agency_id == agency_id, Agent.is_external.is_(False))
            .order_by(Agent.last_name, Agent.first_name)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def list_external_agents(self, agency_id: uuid.UUID) -> list[Agent]:
        stmt = (
            select(Agent)
            .options(selectinload(Agent.role))
            .where(Agent.agency_id == agency_id, Agent.is_external.is_(True))
            .order_by(Agent.last_name, Agent.first_name)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def list_external_roles(self) -> list[Role]:
        stmt = select(Role).where(Role.is_system, Role.is_external.is_(True)).order_by(Role.name)
        return list((await self.db.execute(stmt)).scalars())

    async def list_roles(self, agency_id: uuid.UUID) -> list[Role]:
        """System roles + the agency's customs — minus the system roles
        MASKED by one of the agency's copy-on-write clones (the clone is
        in the list, its origin is not). EXTERNAL roles are never listed
        here (not assignable via the internal flow)."""
        masked = (
            select(Role.cloned_from_role_id)
            .where(Role.agency_id == agency_id, Role.cloned_from_role_id.is_not(None))
            .scalar_subquery()
        )
        stmt = (
            select(Role)
            .where(
                Role.is_external.is_(False),
                or_(
                    Role.agency_id == agency_id,
                    and_(Role.is_system, Role.id.not_in(masked)),
                ),
            )
            .order_by(Role.is_system.desc(), Role.name)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_agent_by_email(self, email: str) -> Agent | None:
        stmt = select(Agent).where(Agent.email == email)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_pending_invitation(
        self, agency_id: uuid.UUID, email: str, now: datetime
    ) -> AgentInvitation | None:
        stmt = select(AgentInvitation).where(
            AgentInvitation.agency_id == agency_id,
            AgentInvitation.email == email,
            AgentInvitation.status == InvitationStatus.PENDING,
            AgentInvitation.expires_at > now,
        )
        return (await self.db.execute(stmt)).scalars().first()

    async def list_invitations(self, agency_id: uuid.UUID) -> list[AgentInvitation]:
        stmt = (
            select(AgentInvitation)
            .where(AgentInvitation.agency_id == agency_id)
            .order_by(AgentInvitation.created_at.desc())
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_invitation_in_agency(
        self, agency_id: uuid.UUID, invitation_id: uuid.UUID
    ) -> AgentInvitation | None:
        stmt = select(AgentInvitation).where(
            AgentInvitation.id == invitation_id,
            AgentInvitation.agency_id == agency_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_invitation_by_token(self, token: str) -> AgentInvitation | None:
        stmt = select(AgentInvitation).where(AgentInvitation.token == token)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_invitation(
        self,
        *,
        agency_id: uuid.UUID,
        email: str,
        role_id: uuid.UUID,
        token: str,
        expires_at: datetime,
        invited_by_agent_id: uuid.UUID,
    ) -> AgentInvitation:
        invitation = AgentInvitation(
            agency_id=agency_id,
            email=email,
            role_id=role_id,
            token=token,
            expires_at=expires_at,
            invited_by_agent_id=invited_by_agent_id,
        )
        self.db.add(invitation)
        return invitation

    def add_agent(
        self,
        *,
        agency_id: uuid.UUID,
        role_id: uuid.UUID,
        email: str,
        first_name: str,
        last_name: str,
        password_hash: str,
        is_external: bool = False,
    ) -> Agent:
        agent = Agent(
            agency_id=agency_id,
            role_id=role_id,
            email=email,
            first_name=first_name,
            last_name=last_name,
            password_hash=password_hash,
            is_external=is_external,
        )
        self.db.add(agent)
        return agent
