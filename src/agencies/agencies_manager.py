import asyncio
import logging
import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.invitation import AgentInvitation
from shared.models.journey import JourneyTemplate
from shared.models.rbac import Role
from shared.models.usage import AgencyUsageMilestone, UsageEvent
from src.agencies.agencies_repository import AgenciesRepository
from src.agencies.agencies_schema import (
    AgencyCreateRequest,
    AgencySubscriptionInfo,
    AgencyUpdateRequest,
    OnboardingResponse,
    OnboardingStepState,
    SeatUsage,
    SubscriptionUpdateRequest,
)
from src.agencies.demo_case_seed import DEMO_JOURNEY_NAME, seed_demo_case
from src.auth.auth_manager import AuthManager
from src.auth.auth_schema import TokenPairResponse
from src.core import storage
from src.core.config import get_settings
from src.core.email import PendingEmail, send_email
from src.core.email_templates import agent_invitation_email, password_reset_email
from src.core.enums import ActorType, Audience, InvitationStatus, SubscriptionPlan
from src.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from src.core.images import process_cover, process_logo
from src.core.rbac.baseline import PLATFORM_ROLE_NAMES
from src.core.security import hash_password
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)

_EMAIL_TAKEN = "This email already has an agent account."

# Structure F (pricing Eric 2026-07-07). Seats bill from the 4th; the
# cap is a hard product limit per plan. An unconverted agency (trial)
# is capped at the 3 included seats of the future base plan.
SEAT_PRICES_EUR = {SubscriptionPlan.CABINET.value: 35, SubscriptionPlan.AGENCE.value: 25}
SEATS_MAX_BY_PLAN = {SubscriptionPlan.CABINET.value: 5, SubscriptionPlan.AGENCE.value: 10}
TRIAL_SEAT_LIMIT = 3


def _slugify(name: str) -> str:
    """Derive a URL-safe slug from an agency name (ASCII, lower, hyphen)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:100].strip("-")


@dataclass(frozen=True)
class AgencyCreated:
    """Result of POST /agencies — the persisted agency + first admin, plus
    the activation email staged for the router to dispatch off-request."""

    agency: Agency
    admin: Agent
    admin_role_name: str
    email: PendingEmail


class AgenciesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = AgenciesRepository(db)

    # --- agency creation (PLATFORM operation, gated agency.create) ----------------

    async def create_agency(self, superadmin: Agent, payload: AgencyCreateRequest) -> AgencyCreated:
        """Create an agency + its first admin ATOMICALLY, then stage one
        activation email. PLATFORM-scoped (superadmin only); introduces NO
        cross-agency access — the new admin, not the superadmin, will work
        the agency. The superadmin still holds only agency.create.
        """
        slug = (payload.slug or _slugify(payload.name)).strip("-")
        if not slug:
            raise ValidationError("Could not derive a slug from the name; provide one explicitly.")
        if await self.repo.get_agency_by_slug(slug) is not None:
            raise ConflictError(f"Agency slug '{slug}' is already taken.")
        # One human = one agent account at MVP (agent.email is table-unique):
        # refuse rather than silently re-attach an agent of another agency.
        if await self.repo.get_agent_by_email(payload.admin_email) is not None:
            raise ConflictError(_EMAIL_TAKEN)
        # The first admin points at the SHARED system 'admin' role
        # (agency_id NULL) — no per-agency role is created.
        admin_role = await self.repo.get_system_role("admin")
        if admin_role is None:
            raise NotFoundError("System role 'admin' is not seeded — run the RBAC baseline seed.")

        if payload.founding_free_seats > 0 and not payload.is_founding:
            raise ValidationError(
                "Free seats are reserved for founding agencies.",
                code="subscription.founding_seats_invalid",
            )
        agency = self.repo.add_agency(
            name=payload.name, slug=slug, default_language=payload.default_language
        )
        # Founding offer posed at creation when Eric already knows it.
        agency.is_founding = payload.is_founding
        agency.founding_free_seats = payload.founding_free_seats
        await self.db.flush()  # need agency.id for the admin row
        admin = self.repo.add_agent(
            agency_id=agency.id,
            role_id=admin_role.id,
            email=payload.admin_email,
            first_name=payload.admin_first_name,
            last_name=payload.admin_last_name,
            # Throwaway: never used. The admin sets their own password via the
            # reset link below (same onboarding as the prod-seed admins).
            password_hash=hash_password(secrets.token_urlsafe(32)),
            is_external=False,
        )
        await self.db.flush()  # need admin.id for the reset token
        # Reuse the password-reset machinery: create_reset_link only STAGES
        # the token (no commit), so agency + admin + token land in ONE
        # transaction — rollback if anything above failed. Onboarding is an
        # INVITATION: 24h window, not the 60-minute forgot-password one.
        settings = get_settings()
        reset_link = AuthManager(self.db).create_reset_link(
            admin.id, Audience.AGENT, expires_minutes=settings.onboarding_link_expires_minutes
        )
        # Usage trackers: the wizard starts the trial clock and emits
        # the adoption anchor event.
        agency.trial_ends_at = datetime.now(UTC) + timedelta(days=settings.trial_days)
        await UsageManager(self.db).emit(
            agency_id=agency.id,
            event_type="agency.activated",
            actor_type=ActorType.AGENT,
            actor_id=superadmin.id,
        )
        await self.db.commit()
        await self.db.refresh(agency)
        await self.db.refresh(admin)

        # The example dossier (nurture bloc 2): a best-effort GIFT in its
        # own transaction, AFTER the atomic wizard commit — a seed failure
        # (storage down, whatever) must never cost an agency creation.
        try:
            await seed_demo_case(self.db, agency, admin)
        except Exception:
            await self.db.rollback()
            # Rollback expires the loaded rows — re-fetch before the
            # response serialization touches their attributes.
            await self.db.refresh(agency)
            await self.db.refresh(admin)
            logger.exception("demo case seed failed for agency %s", agency.slug)

        content = password_reset_email(reset_link, settings.onboarding_link_expires_minutes)
        email = PendingEmail(
            to=payload.admin_email,
            subject=content.subject,
            text=content.text,
            html=content.html,
        )
        return AgencyCreated(
            agency=agency, admin=admin, admin_role_name=admin_role.name, email=email
        )

    # --- logo (branding) --------------------------------------------------------------

    async def upload_logo(self, agent: Agent, content_type: str | None, raw: bytes) -> Agency:
        """Shared image pipeline, logo flavor: bounded 1024px wide, ratio
        kept, PNG preserved on alpha. The previous blob is ALWAYS deleted
        first: Supabase refuses a same-path overwrite (409 Duplicate), so
        a PNG→PNG replacement would 500 without it (prod bug, 2026-07-03)."""
        agency = await self.get_my_agency(agent)
        processed, media_type = process_logo(content_type, raw)
        extension = "png" if media_type == "image/png" else "jpg"
        path = f"logos/agency/{agency.id}.{extension}"
        if agency.logo_path is not None:
            storage.delete(agency.logo_path)
        storage.upload(path, processed, media_type)
        await UsageManager(self.db).emit(
            agency_id=agency.id,
            event_type="agency.branding_updated",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
        )
        agency.logo_path = path
        await self.db.commit()
        await self.db.refresh(agency)
        return agency

    async def delete_logo(self, agent: Agent) -> Agency:
        agency = await self.get_my_agency(agent)
        if agency.logo_path is not None:
            storage.delete(agency.logo_path)
            agency.logo_path = None
            await self.db.commit()
            await self.db.refresh(agency)
        return agency

    @staticmethod
    def logo_bytes(agency: Agency) -> tuple[bytes, str]:
        """(content, media_type) of the stored logo — 404 when absent.
        Callers OWN the scoping (own agency / live case / public slug)."""
        if agency.logo_path is None:
            raise NotFoundError("Logo not found.")
        media_type = "image/png" if agency.logo_path.endswith(".png") else "image/jpeg"
        return storage.download(agency.logo_path), media_type

    # --- cover (branding, same family as the logo) ---------------------------------

    async def upload_cover(self, agent: Agent, content_type: str | None, raw: bytes) -> Agency:
        """Shared image pipeline, cover flavor: center-cropped 4:1 banner,
        2560px wide max, always JPEG. The path is constant per agency, so
        the previous blob is deleted first (re-upload = clean overwrite)."""
        agency = await self.get_my_agency(agent)
        processed = process_cover(content_type, raw)
        path = f"covers/agency/{agency.id}.jpg"
        if agency.cover_path is not None:
            storage.delete(agency.cover_path)
        storage.upload(path, processed, "image/jpeg")
        await UsageManager(self.db).emit(
            agency_id=agency.id,
            event_type="agency.branding_updated",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
        )
        agency.cover_path = path
        await self.db.commit()
        await self.db.refresh(agency)
        return agency

    async def delete_cover(self, agent: Agent) -> Agency:
        agency = await self.get_my_agency(agent)
        if agency.cover_path is not None:
            storage.delete(agency.cover_path)
            agency.cover_path = None
            await self.db.commit()
            await self.db.refresh(agency)
        return agency

    @staticmethod
    def cover_bytes(agency: Agency) -> tuple[bytes, str]:
        """(content, media_type) of the stored cover — 404 when absent.
        Callers OWN the scoping, exactly like logo_bytes."""
        if agency.cover_path is None:
            raise NotFoundError("Cover not found.")
        return storage.download(agency.cover_path), "image/jpeg"

    async def public_logo_by_slug(self, slug: str) -> tuple[bytes, str]:
        """THE assumed public exception (client login page): image bytes
        only — an unknown slug and a logo-less agency answer the SAME 404,
        and no metadata ever leaves."""
        agency = await self.repo.get_agency_by_slug(slug)
        if agency is None:
            raise NotFoundError("Logo not found.")
        return self.logo_bytes(agency)

    # --- agency (tenant scoping: always from the token, never from the URL) ----

    async def get_my_agency(self, agent: Agent) -> Agency:
        agency = await self.repo.get_agency(agent.agency_id)
        if agency is None:
            raise NotFoundError("Agency not found.")
        return agency

    # --- subscription (structure F, manual billing) ----------------------------------

    async def seat_usage(self, agency: Agency) -> SeatUsage:
        """Derived seat capacity: active INTERNAL members only (external
        providers never consume a seat). `billed` counts past the
        included + founding-offered seats - Eric bills those manually,
        the app never blocks paid usage below the plan cap."""
        members = (
            await self.db.execute(
                select(func.count(Agent.id)).where(
                    Agent.agency_id == agency.id, Agent.is_external.is_(False)
                )
            )
        ).scalar_one()
        return SeatUsage(
            members=members,
            included=agency.seats_included,
            offered=agency.founding_free_seats,
            billed=max(0, members - agency.seats_included - agency.founding_free_seats),
            max=SEATS_MAX_BY_PLAN.get(agency.plan or "", TRIAL_SEAT_LIMIT),
        )

    async def subscription_info(self, agency: Agency) -> AgencySubscriptionInfo:
        return AgencySubscriptionInfo(
            plan=agency.plan,
            billing_cycle=agency.billing_cycle,
            is_founding=agency.is_founding,
            seats=await self.seat_usage(agency),
        )

    async def update_subscription(
        self, superadmin: Agent, agency_id: uuid.UUID, payload: SubscriptionUpdateRequest
    ) -> AgencySubscriptionInfo:
        """Eric's post-closing gesture (superadmin only): pose the plan,
        cycle, founding terms and conversion date. Setting the plan
        derives seat_price_eur and stamps converted_at when absent;
        trial_ends_at is NEVER touched here (pre-conversion marker)."""
        agency = await self.repo.get_agency(agency_id)
        if agency is None:
            raise NotFoundError("Agency not found.")
        if payload.is_founding is not None:
            agency.is_founding = payload.is_founding
        if payload.founding_free_seats is not None:
            if payload.founding_free_seats > 0 and not agency.is_founding:
                raise ValidationError(
                    "Free seats are reserved for founding agencies.",
                    code="subscription.founding_seats_invalid",
                )
            agency.founding_free_seats = payload.founding_free_seats
        if payload.plan is not None:
            agency.plan = payload.plan.value
            agency.seat_price_eur = SEAT_PRICES_EUR[payload.plan.value]
        if payload.billing_cycle is not None:
            agency.billing_cycle = payload.billing_cycle.value
        if payload.price_locked_until is not None:
            agency.price_locked_until = payload.price_locked_until
        if payload.converted_at is not None:
            agency.converted_at = payload.converted_at
        elif payload.plan is not None and agency.converted_at is None:
            agency.converted_at = datetime.now(UTC)
        if payload.plan is not None:
            await UsageManager(self.db).emit(
                agency_id=agency.id,
                event_type="agency.converted",
                actor_type=ActorType.AGENT,
                actor_id=superadmin.id,
                details={
                    "plan": agency.plan,
                    "billing_cycle": agency.billing_cycle,
                    "is_founding": agency.is_founding,
                },
            )
        await self.db.commit()
        await self.db.refresh(agency)
        return await self.subscription_info(agency)

    # --- onboarding checklist (activation) ------------------------------------------

    async def onboarding_state(self, agent: Agent) -> OnboardingResponse:
        """The activation checklist, computed LIVE from the milestones
        and events (zero checkbox state - the trackers are the truth):
        - create_journey: milestone premier_parcours_cree (demo excluded
          as always) OR any agency template besides the seeded demo gift
          (covers histories predating the import/clone milestone fix);
        - open_case: milestone premier_dossier_cree OR the closest
          existing trace of a demo consultation - the case.viewed_as_client
          event (a plain GET leaves no trace BY DESIGN, no new tracker);
        - view_as_client: the case.viewed_as_client event."""
        agency = await self.get_my_agency(agent)
        rows = (
            await self.db.execute(
                select(AgencyUsageMilestone.key, AgencyUsageMilestone.first_at).where(
                    AgencyUsageMilestone.agency_id == agent.agency_id,
                    AgencyUsageMilestone.key.in_(["premier_parcours_cree", "premier_dossier_cree"]),
                )
            )
        ).all()
        firsts: dict[str, datetime] = {row.key: row.first_at for row in rows}
        journey_at = firsts.get("premier_parcours_cree")
        if journey_at is None:
            journey_at = (
                await self.db.execute(
                    select(func.min(JourneyTemplate.created_at)).where(
                        JourneyTemplate.agency_id == agent.agency_id,
                        JourneyTemplate.name != DEMO_JOURNEY_NAME,
                    )
                )
            ).scalar_one_or_none()
        viewed_at = (
            await self.db.execute(
                select(func.min(UsageEvent.created_at)).where(
                    UsageEvent.agency_id == agent.agency_id,
                    UsageEvent.event_type == "case.viewed_as_client",
                )
            )
        ).scalar_one_or_none()
        open_at = firsts.get("premier_dossier_cree") or viewed_at
        return OnboardingResponse(
            steps=[
                OnboardingStepState(
                    key="create_journey", done=journey_at is not None, done_at=journey_at
                ),
                OnboardingStepState(key="open_case", done=open_at is not None, done_at=open_at),
                OnboardingStepState(
                    key="view_as_client", done=viewed_at is not None, done_at=viewed_at
                ),
            ],
            dismissed=agency.onboarding_dismissed_at is not None,
        )

    async def dismiss_onboarding(self, agent: Agent) -> OnboardingResponse:
        """Persist the dismiss (once - no un-dismiss) and return the
        state the front should render."""
        agency = await self.get_my_agency(agent)
        if agency.onboarding_dismissed_at is None:
            agency.onboarding_dismissed_at = datetime.now(UTC)
            await self.db.commit()
        return await self.onboarding_state(agent)

    async def update_my_agency(self, agent: Agent, payload: AgencyUpdateRequest) -> Agency:
        agency = await self.get_my_agency(agent)
        if payload.name is not None:
            agency.name = payload.name
        if payload.settings is not None:
            agency.settings = payload.settings
        if payload.default_language is not None:
            agency.default_language = payload.default_language
        await self.db.commit()
        await self.db.refresh(agency)
        return agency

    # --- platform: cross-tenant agency listing (superadmin only) ------------------

    async def list_all_agencies(self) -> list[Agency]:
        """ALL agencies — the platform agency switcher. Superadmin-only: the
        route is gated agency.create, the one permission no agency role holds.
        """
        return await self.repo.list_all_agencies()

    # --- members & roles (tenant reference lists, no permission gate) -------------

    async def list_members(self, agent: Agent) -> list[Agent]:
        return await self.repo.list_agents_with_roles(agent.agency_id)

    async def list_roles(self, agent: Agent) -> list[Role]:
        return await self.repo.list_roles(agent.agency_id)

    # --- external providers (wave A) ---------------------------------------------

    async def list_external_roles(self, agent: Agent) -> list[Role]:
        return await self.repo.list_external_roles()

    async def list_external_members(self, agent: Agent) -> list[Agent]:
        return await self.repo.list_external_agents(agent.agency_id)

    async def create_external_invitation(
        self, agent: Agent, email: str, role_id: uuid.UUID
    ) -> AgentInvitation:
        return await self._create_invitation(agent, email, role_id, external=True)

    # --- agent invitations -------------------------------------------------------

    async def create_invitation(
        self, agent: Agent, email: str, role_id: uuid.UUID
    ) -> AgentInvitation:
        return await self._create_invitation(agent, email, role_id, external=False)

    async def _create_invitation(
        self, agent: Agent, email: str, role_id: uuid.UUID, *, external: bool
    ) -> AgentInvitation:
        # Role validated AT CREATION (not only at accept): system role
        # OR a role of THIS agency — never another agency's role.
        role = await self.repo.get_role(role_id)
        if role is None or (not role.is_system and role.agency_id != agent.agency_id):
            raise ValidationError("Role does not exist or does not belong to this agency.")
        # Platform-reserved (superadmin): granted ONLY via the seed, never
        # invitable. Closes the escalation path — this flow has no permission
        # ceiling (unlike member-role assignment), so without this an agency
        # admin could invite a superadmin. Opaque message: don't reveal it.
        if role.name in PLATFORM_ROLE_NAMES:
            raise ValidationError("Role does not exist or does not belong to this agency.")
        # The two flows never cross: an external role only via the external
        # endpoint, an internal role only via the internal one.
        if external and not role.is_external:
            raise ValidationError("This endpoint requires one of the external provider roles.")
        if not external and role.is_external:
            raise ValidationError("External roles are invited via the external-invitation flow.")
        # SEAT GATE (decision flagged, pricing 2026-07-07): an INTERNAL
        # invitation is blocked only at the plan's hard cap (5 cabinet,
        # 10 agence; 3 on trial = the future included seats). Between
        # included+offered and the cap the invitation goes THROUGH:
        # billing is manual (Eric bills), the app never blocks paid
        # usage. Externals never consume a seat. Members are counted,
        # not pending invitations (an accepted invite past the cap is a
        # manual-billing tolerance, not a hole).
        if not external:
            agency = await self.get_my_agency(agent)
            usage = await self.seat_usage(agency)
            if usage.members >= usage.max:
                message = (
                    f"The {agency.plan} plan is capped at {usage.max} members."
                    if agency.plan
                    else "The trial is capped at 3 members; converting to a plan unlocks more."
                )
                raise ConflictError(
                    message,
                    code="subscription.seat_limit",
                    params={"members": usage.members, "max": usage.max, "plan": agency.plan},
                )

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
        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="provider.invited" if external else "member.invited",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"email": email},
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
        # is_external is DERIVED from the role (the denormalized filter).
        role = await self.repo.get_role(invitation.role_id)
        agent = self.repo.add_agent(
            agency_id=invitation.agency_id,
            role_id=invitation.role_id,
            email=invitation.email,
            first_name=first_name,
            last_name=last_name,
            password_hash=hash_password(password),
            is_external=bool(role and role.is_external),
        )
        await self.db.flush()
        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_at = now

        if not (role and role.is_external):
            await UsageManager(self.db).emit(
                agency_id=invitation.agency_id,
                event_type="member.activated",
                actor_type=ActorType.AGENT,
                actor_id=agent.id,
            )
        pair = AuthManager(self.db).issue_token_pair(agent.id, Audience.AGENT)
        await self.db.commit()
        return pair
