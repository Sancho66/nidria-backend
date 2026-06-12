import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.auth_tokens import PasswordResetToken, RefreshToken
from shared.models.expat_user import ExpatUser
from shared.models.invitation import CaseInvitation
from shared.models.rbac import Role


class AuthRepository:
    """Pure DB access for the auth flows — no business logic, no commit
    (the Manager owns the transaction)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- identities ----------------------------------------------------------

    async def get_agent_by_email(self, email: str) -> Agent | None:
        stmt = (
            select(Agent)
            .where(Agent.email == email)
            .options(selectinload(Agent.role).selectinload(Role.permissions))
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_agent(self, agent_id: uuid.UUID) -> Agent | None:
        return await self.db.get(Agent, agent_id)

    async def get_expat_by_email(self, email: str) -> ExpatUser | None:
        stmt = select(ExpatUser).where(ExpatUser.email == email)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_expat(self, expat_id: uuid.UUID) -> ExpatUser | None:
        return await self.db.get(ExpatUser, expat_id)

    # --- refresh tokens --------------------------------------------------------

    def add_refresh_token(
        self,
        jti: uuid.UUID,
        actor_type: str,
        actor_id: uuid.UUID,
        expires_at: datetime,
    ) -> RefreshToken:
        row = RefreshToken(jti=jti, actor_type=actor_type, actor_id=actor_id, expires_at=expires_at)
        self.db.add(row)
        return row

    async def get_refresh_token(self, jti: uuid.UUID) -> RefreshToken | None:
        return await self.db.get(RefreshToken, jti)

    async def revoke_all_active_refresh_tokens(
        self, actor_type: str, actor_id: uuid.UUID, now: datetime
    ) -> None:
        await self.db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.actor_type == actor_type,
                RefreshToken.actor_id == actor_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    # --- password reset tokens ---------------------------------------------------

    def add_reset_token(
        self,
        actor_type: str,
        actor_id: uuid.UUID,
        token: str,
        expires_at: datetime,
    ) -> PasswordResetToken:
        row = PasswordResetToken(
            actor_type=actor_type, actor_id=actor_id, token=token, expires_at=expires_at
        )
        self.db.add(row)
        return row

    async def get_reset_token(self, token: str) -> PasswordResetToken | None:
        stmt = select(PasswordResetToken).where(PasswordResetToken.token == token)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    # --- invitations ---------------------------------------------------------------

    async def get_case_invitation_by_token(self, token: str) -> CaseInvitation | None:
        stmt = select(CaseInvitation).where(CaseInvitation.token == token)
        return (await self.db.execute(stmt)).scalar_one_or_none()
