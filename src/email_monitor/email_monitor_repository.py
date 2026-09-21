"""Storage for the single Resend account shared by every tenant."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from shared.models.agent import Agent
from shared.models.email_account_state import EmailAccountState
from shared.models.rbac import Role
from src.core.rbac.baseline import PLATFORM_ROLE_NAMES


class EmailMonitorRepository:
    @staticmethod
    def locked(db: Session) -> EmailAccountState:
        db.execute(
            insert(EmailAccountState).values(provider="resend", state={}).on_conflict_do_nothing()
        )
        return db.execute(
            select(EmailAccountState)
            .where(EmailAccountState.provider == "resend")
            .with_for_update()
        ).scalar_one()

    @staticmethod
    def operator(db: Session) -> Agent | None:
        return db.scalars(
            select(Agent)
            .join(Role, Agent.role_id == Role.id)
            .where(
                Role.is_system.is_(True),
                Role.name.in_(PLATFORM_ROLE_NAMES),
                Agent.deactivated_at.is_(None),
            )
            .order_by(Agent.id)
            .limit(1)
        ).first()

    @staticmethod
    async def read(db: AsyncSession) -> EmailAccountState | None:
        return await db.get(EmailAccountState, "resend")
