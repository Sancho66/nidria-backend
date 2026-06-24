import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.config import get_settings
from src.core.enums import ActorType, Audience
from src.core.exceptions import NotFoundError, ValidationError
from src.core.security import create_access_token
from src.impersonation.impersonation_repository import ImpersonationRepository
from src.impersonation.impersonation_schema import ImpersonationTokenResponse


class ImpersonationManager:
    """Issues short-lived 'see what they see' tokens.

    Chaining is denied centrally (enforcement: claim present → 403 on
    both endpoints), so an actor here is never itself impersonated.
    The token carries the TARGET as subject — effective permissions are
    the target's, never the impersonator's (no elevation by design)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ImpersonationRepository(db)

    async def impersonate_agent(
        self, actor: Agent, target_agent_id: uuid.UUID
    ) -> ImpersonationTokenResponse:
        if target_agent_id == actor.id:
            raise ValidationError("You cannot impersonate yourself.")
        target = await self.repo.get_agent_in_agency(actor.agency_id, target_agent_id)
        if target is None:
            raise NotFoundError("Agent not found.")
        return await self._issue(actor, Audience.AGENT, ActorType.AGENT, target.id)

    async def enter_agency(
        self, actor: Agent, agency_id: uuid.UUID
    ) -> ImpersonationTokenResponse:
        """Platform agency switcher — superadmin-only (the route is gated
        agency.create). Step into ANOTHER agency by impersonating one of its
        admins: full control of that agency, scoped naturally (the token's
        subject is a real internal agent of it, so every WHERE agency_id holds
        without touching the tenant layer). Own agency is a no-op (use Exit).
        No chaining — the actor is never itself impersonated (denied centrally).
        """
        if agency_id == actor.agency_id:
            raise ValidationError("You are already in this agency.")
        target = await self.repo.get_an_admin_of_agency(agency_id)
        if target is None:
            raise NotFoundError("This agency has no administrator to enter as.")
        return await self._issue(actor, Audience.AGENT, ActorType.AGENT, target.id)

    async def impersonate_expat(
        self, actor: Agent, expat_user_id: uuid.UUID
    ) -> ImpersonationTokenResponse:
        """Scoped to the agency's clientele: the expat must be principal
        of at least one of the actor's agency cases — 'see what your
        client sees', not a cross-agency master key."""
        expat = await self.repo.get_expat(expat_user_id)
        if expat is None or not await self.repo.expat_is_principal_in_agency(
            expat_user_id, actor.agency_id
        ):
            raise NotFoundError("Expat user not found.")
        return await self._issue(actor, Audience.EXPAT, ActorType.EXPAT, expat_user_id)

    async def _issue(
        self,
        actor: Agent,
        audience: Audience,
        target_type: ActorType,
        target_id: uuid.UUID,
    ) -> ImpersonationTokenResponse:
        minutes = get_settings().impersonation_token_expires_minutes
        token = create_access_token(
            str(target_id),
            audience,
            extra_claims={"impersonator_id": str(actor.id)},
            expires_minutes=minutes,
        )
        self.repo.add_log(
            impersonator_agent_id=actor.id,
            target_type=target_type.value,
            target_id=target_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
        )
        await self.db.commit()
        return ImpersonationTokenResponse(
            access_token=token, audience=audience.value, expires_in_minutes=minutes
        )
