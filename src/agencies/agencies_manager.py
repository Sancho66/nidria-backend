import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.invitation import AgentInvitation
from shared.models.rbac import Role
from src.agencies.agencies_repository import AgenciesRepository
from src.agencies.agencies_schema import AgencyUpdateRequest
from src.auth.auth_manager import AuthManager
from src.auth.auth_schema import TokenPairResponse
from src.core.config import get_settings
from src.core.email import send_email
from src.core.email_templates import agent_invitation_email
from src.core.enums import Audience, InvitationStatus
from src.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from src.core.security import hash_password

_EMAIL_TAKEN = "This email already has an agent account."


class AgenciesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = AgenciesRepository(db)

    # --- agency (tenant scoping: always from the token, never from the URL) ----

    async def get_my_agency(self, agent: Agent) -> Agency:
        agency = await self.repo.get_agency(agent.agency_id)
        if agency is None:
            raise NotFoundError("Agency not found.")
        return agency

    async def update_my_agency(self, agent: Agent, payload: AgencyUpdateRequest) -> Agency:
        agency = await self.get_my_agency(agent)
        if payload.name is not None:
            agency.name = payload.name
        if payload.settings is not None:
            agency.settings = payload.settings
        await self.db.commit()
        await self.db.refresh(agency)
        return agency

    # --- members & roles (tenant reference lists, no permission gate) -------------

    async def list_members(self, agent: Agent) -> list[Agent]:
        return await self.repo.list_agents_with_roles(agent.agency_id)

    async def list_roles(self, agent: Agent) -> list[Role]:
        return await self.repo.list_roles(agent.agency_id)

    # --- agent invitations -------------------------------------------------------

    async def create_invitation(
        self, agent: Agent, email: str, role_id: uuid.UUID
    ) -> AgentInvitation:
        # Role validated AT CREATION (not only at accept): system role
        # OR a role of THIS agency — never another agency's role.
        role = await self.repo.get_role(role_id)
        if role is None or (not role.is_system and role.agency_id != agent.agency_id):
            raise ValidationError("Role does not exist or does not belong to this agency.")

        # One human = one agent account = one agency at MVP
        # (agent.email is table-unique); refuse at creation, whichever
        # agency the existing account belongs to.
        if await self.repo.get_agent_by_email(email) is not None:
            raise ConflictError(_EMAIL_TAKEN)

        now = datetime.now(UTC)
        if await self.repo.get_pending_invitation(agent.agency_id, email, now) is not None:
            raise ConflictError("An invitation is already pending for this email.")

        settings = get_settings()
        invitation = self.repo.add_invitation(
            agency_id=agent.agency_id,
            email=email,
            role_id=role_id,
            token=secrets.token_urlsafe(24),
            expires_at=now + timedelta(days=settings.agent_invitation_expires_days),
            invited_by_agent_id=agent.id,
        )
        await self.db.commit()
        await self.db.refresh(invitation)

        agency = await self.get_my_agency(agent)
        link = f"{settings.frontend_url}/accept-invitation/{invitation.token}"
        content = agent_invitation_email(agency.name, link, settings.agent_invitation_expires_days)
        await asyncio.to_thread(send_email, email, content.subject, content.text, content.html)
        return invitation

    async def list_invitations(self, agent: Agent) -> list[AgentInvitation]:
        return await self.repo.list_invitations(agent.agency_id)

    async def cancel_invitation(self, agent: Agent, invitation_id: uuid.UUID) -> None:
        invitation = await self.repo.get_invitation_in_agency(agent.agency_id, invitation_id)
        if invitation is None:
            raise NotFoundError("Invitation not found.")
        if invitation.status != InvitationStatus.PENDING:
            raise ConflictError("Only pending invitations can be cancelled.")
        invitation.status = InvitationStatus.CANCELLED
        await self.db.commit()

    async def accept_invitation(
        self, *, token: str, password: str, first_name: str, last_name: str
    ) -> TokenPairResponse:
        invitation = await self.repo.get_invitation_by_token(token)
        now = datetime.now(UTC)
        if (
            invitation is None
            or invitation.status != InvitationStatus.PENDING
            or invitation.expires_at <= now
        ):
            raise BadRequestError("Invalid or expired invitation token.")

        # Re-check at accept: the email may have become an agent
        # between invite and accept.
        if await self.repo.get_agent_by_email(invitation.email) is not None:
            raise ConflictError(_EMAIL_TAKEN)

        # The agent is created in the INVITATION's agency — never in
        # any context derived from the caller. Single-role model: the
        # invitation's role_id becomes the agent's role directly.
        agent = self.repo.add_agent(
            agency_id=invitation.agency_id,
            role_id=invitation.role_id,
            email=invitation.email,
            first_name=first_name,
            last_name=last_name,
            password_hash=hash_password(password),
        )
        await self.db.flush()
        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_at = now

        pair = AuthManager(self.db).issue_token_pair(agent.id, Audience.AGENT)
        await self.db.commit()
        return pair
