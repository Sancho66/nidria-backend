import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Row, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.auth_tokens import PasswordResetToken, RefreshToken
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.external_contact import ExternalContact
from shared.models.invitation import AgentInvitation
from shared.models.journey import (
    JourneyStepAttachment,
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateStep,
)
from shared.models.mfa import MfaChallenge, MfaTotp
from shared.models.rbac import Role
from src.core.enums import ActorType, InvitationStatus
from src.core.rbac.baseline import PLATFORM_ROLE_NAMES


class AgenciesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_agency(self, agency_id: uuid.UUID) -> Agency | None:
        return await self.db.get(Agency, agency_id)

    async def list_all_agencies(self) -> list[Agency]:
        """EVERY agency, platform-wide. The single read that deliberately
        crosses the tenant boundary — for the superadmin agency switcher
        (gated agency.create at the route; no agency role ever reaches it)."""
        stmt = select(Agency).order_by(Agency.name)
        return list((await self.db.execute(stmt)).scalars())

    async def get_agency_by_slug(self, slug: str) -> Agency | None:
        stmt = select(Agency).where(Agency.slug == slug)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_agency(self, *, name: str, slug: str, default_language: str) -> Agency:
        agency = Agency(name=name, slug=slug, default_language=default_language, settings={})
        self.db.add(agency)
        return agency

    def add_directory_contact(
        self, *, agency_id: uuid.UUID, name: str, email: str | None, phone: str | None, type: str
    ) -> ExternalContact:
        """An AGENCY DIRECTORY external contact: case_id NULL, agent_id NULL
        (no login yet). The partial unique (agency_id, lower(name)) WHERE
        case_id IS NULL guards duplicates at the DB level."""
        contact = ExternalContact(
            agency_id=agency_id, case_id=None, name=name, email=email, phone=phone, type=type
        )
        self.db.add(contact)
        return contact

    # --- hard delete (Groupe C) ------------------------------------------------------

    async def count_active_non_demo_cases(self, agency_id: uuid.UUID) -> int:
        """The 409 guardrail: live (not soft-deleted), real (non-demo)
        cases. Demo cases never block a deletion."""
        stmt = select(func.count(ClientCase.id)).where(
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
            ClientCase.is_demo.is_(False),
        )
        return (await self.db.execute(stmt)).scalar_one()

    async def count_all_cases(self, agency_id: uuid.UUID) -> int:
        """Every case of the agency (demo + soft-deleted included): what
        the hard delete actually removes, for the trace."""
        stmt = select(func.count(ClientCase.id)).where(ClientCase.agency_id == agency_id)
        return (await self.db.execute(stmt)).scalar_one()

    async def agent_ids(self, agency_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = select(Agent.id).where(Agent.agency_id == agency_id)
        return list((await self.db.execute(stmt)).scalars())

    async def storage_paths(self, agency_id: uuid.UUID) -> list[str]:
        """Every blob to purge for the agency, gathered BEFORE the rows
        cascade away (the storage has no prefix delete; paths are keyed
        by object id, never by agency). Expat avatars are NOT here - they
        belong to the global client identity, shared across agencies."""
        agency = await self.db.get(Agency, agency_id)
        paths: list[str] = []
        if agency is not None:
            paths += [p for p in (agency.logo_path, agency.cover_path) if p]
        paths += [
            p
            for (p,) in (
                await self.db.execute(
                    select(Agent.avatar_path).where(
                        Agent.agency_id == agency_id, Agent.avatar_path.is_not(None)
                    )
                )
            ).all()
            if p
        ]
        paths += list(
            (
                await self.db.execute(
                    select(Document.storage_path)
                    .join(ClientCase, ClientCase.id == Document.case_id)
                    .where(ClientCase.agency_id == agency_id)
                )
            ).scalars()
        )
        paths += list(
            (
                await self.db.execute(
                    select(JourneyStepAttachment.storage_path)
                    .join(
                        JourneyTemplateStep,
                        JourneyTemplateStep.id == JourneyStepAttachment.step_id,
                    )
                    .join(JourneyTemplate, JourneyTemplate.id == JourneyTemplateStep.template_id)
                    .where(JourneyTemplate.agency_id == agency_id)
                )
            ).scalars()
        )
        return paths

    async def purge_agency_rows(self, agency_id: uuid.UUID, agent_ids: list[uuid.UUID]) -> None:
        """Ordered hard delete (NO commit — the manager owns the tx).
        Breaks the 4 RESTRICT edges by deleting the referencing rows
        FIRST, cleans the no-FK polymorphic token tables by hand, then
        DELETEs the agency (CASCADE clears everything else). Consent
        acceptances (no FK, legal trace) survive by design."""
        # Polymorphic no-FK tables: keyed by the agency's AGENT ids only
        # (expat tokens are global — the client keeps sessions for other
        # agencies).
        if agent_ids:
            agents = ActorType.AGENT.value
            for model in (RefreshToken, PasswordResetToken, MfaTotp, MfaChallenge):
                await self.db.execute(
                    delete(model).where(model.actor_type == agents, model.actor_id.in_(agent_ids))
                )
        # RESTRICT edges (client_case→journey_template,
        # case_step_progress→journey_template_step, agent→role,
        # agent_invitation→role): delete the referencing rows before the
        # agency cascade would hit the referenced ones in an unsafe order.
        await self.db.execute(delete(ClientCase).where(ClientCase.agency_id == agency_id))
        await self.db.execute(delete(AgentInvitation).where(AgentInvitation.agency_id == agency_id))
        await self.db.execute(delete(Agent).where(Agent.agency_id == agency_id))
        # The agency itself: CASCADE clears journeys, custom fields,
        # custom roles, usage, milestones, ai usage/jobs, nurture, crm
        # mappings, message templates, saved views...
        await self.db.execute(delete(Agency).where(Agency.id == agency_id))

    async def get_role(self, role_id: uuid.UUID) -> Role | None:
        return await self.db.get(Role, role_id)

    async def get_system_role(self, name: str) -> Role | None:
        """A shared platform role by name (agency_id NULL, is_system) — e.g.
        'admin' for a new agency's first admin. Never an agency clone."""
        stmt = select(Role).where(Role.is_system, Role.agency_id.is_(None), Role.name == name)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        stmt = select(Agent).where(Agent.id == agent_id, Agent.agency_id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_owned_case_ids(
        self, agency_id: uuid.UUID, agent_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Non-deleted cases owned by the agent — the offboarding inventory."""
        stmt = select(ClientCase.id).where(
            ClientCase.agency_id == agency_id,
            ClientCase.owner_agent_id == agent_id,
            ClientCase.deleted_at.is_(None),
        )
        return list((await self.db.execute(stmt)).scalars())

    async def list_responsible_active_steps(
        self, agency_id: uuid.UUID, agent_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(case_id, progress_id) of ACTIVE steps (todo/in_progress) whose
        responsible is the agent — DONE steps are history, never reassigned."""
        stmt = (
            select(CaseStepProgress.case_id, CaseStepProgress.id)
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .where(
                ClientCase.agency_id == agency_id,
                ClientCase.deleted_at.is_(None),
                CaseStepProgress.responsible_agent_id == agent_id,
                CaseStepProgress.status != "done",
            )
        )
        return [(row[0], row[1]) for row in (await self.db.execute(stmt)).all()]

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
        in the list, its origin is not). EXTERNAL and PLATFORM-reserved
        (superadmin) roles are never listed here (not assignable via the
        internal flow)."""
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
                    and_(
                        Role.is_system,
                        Role.id.not_in(masked),
                        # Platform-reserved roles (superadmin) are never
                        # offered to an agency — not listable, not assignable.
                        Role.name.not_in(PLATFORM_ROLE_NAMES),
                    ),
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
        external_contact_id: uuid.UUID | None = None,
    ) -> AgentInvitation:
        invitation = AgentInvitation(
            agency_id=agency_id,
            email=email,
            role_id=role_id,
            token=token,
            expires_at=expires_at,
            invited_by_agent_id=invited_by_agent_id,
            external_contact_id=external_contact_id,
        )
        self.db.add(invitation)
        return invitation

    async def get_directory_contact(
        self, agency_id: uuid.UUID, contact_id: uuid.UUID
    ) -> ExternalContact | None:
        stmt = select(ExternalContact).where(
            ExternalContact.id == contact_id,
            ExternalContact.agency_id == agency_id,
            ExternalContact.case_id.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_directory_contacts(self, agency_id: uuid.UUID) -> list[Row[Any]]:
        """Directory rows (case_id IS NULL) + the designated account's role
        name (NULL if no account) + the count of TEMPLATE step participations
        (what a delete would SET NULL) + the PENDING invitation's created_at
        (invited_at, NULL if none — the authority for access_state). ONE query,
        no N+1."""
        participations = (
            select(func.count())
            .select_from(JourneyStepParticipant)
            .where(JourneyStepParticipant.external_id == ExternalContact.id)
            .correlate(ExternalContact)
            .scalar_subquery()
        )
        # invited_at = the PENDING invitation's created_at (authority for
        # 'invited' vs 'active': a pending invitation means not-yet-activated).
        invited_at = (
            select(AgentInvitation.created_at)
            .where(
                AgentInvitation.external_contact_id == ExternalContact.id,
                AgentInvitation.status == InvitationStatus.PENDING.value,
            )
            .correlate(ExternalContact)
            .order_by(AgentInvitation.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            select(
                ExternalContact.id,
                ExternalContact.name,
                ExternalContact.email,
                ExternalContact.phone,
                ExternalContact.type,
                ExternalContact.agent_id,
                Role.name.label("agent_role"),
                participations.label("used_in_steps"),
                invited_at.label("invited_at"),
            )
            .select_from(ExternalContact)
            .outerjoin(Agent, Agent.id == ExternalContact.agent_id)
            .outerjoin(Role, Role.id == Agent.role_id)
            .where(ExternalContact.agency_id == agency_id, ExternalContact.case_id.is_(None))
            .order_by(func.lower(ExternalContact.name))
        )
        return list((await self.db.execute(stmt)).all())

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
