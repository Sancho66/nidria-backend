import asyncio
import logging
import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agency_deletion_log import AgencyDeletionLog
from shared.models.agent import Agent
from shared.models.external_contact import ExternalContact
from shared.models.invitation import AgentInvitation
from shared.models.journey import JourneyTemplate
from shared.models.rbac import Role
from shared.models.usage import AgencyUsageMilestone, UsageEvent
from src.agencies.agencies_repository import AgenciesRepository
from src.agencies.agencies_schema import (
    AgencyCreateRequest,
    AgencyDeletedResponse,
    AgencyDeleteRequest,
    AgencySubscriptionInfo,
    AgencyUpdateRequest,
    DirectoryContactCreateRequest,
    DirectoryContactListItem,
    MemberDeactivationResponse,
    OnboardingResponse,
    OnboardingStepState,
    ProviderUsage,
    ResponsibleStepRef,
    SeatUsage,
    SubscriptionUpdateRequest,
)
from src.agencies.demo_case_seed import DEMO_JOURNEY_NAME, seed_demo_case
from src.auth.auth_manager import AuthManager
from src.auth.auth_schema import TokenPairResponse
from src.core import storage
from src.core.config import get_settings
from src.core.currencies import default_currency_for_language
from src.core.email import PendingEmail, send_email
from src.core.email_templates import agent_invitation_email, password_reset_email
from src.core.enums import (
    ActorType,
    AgencySector,
    Audience,
    ExternalContactType,
    InvitationStatus,
    SubscriptionPlan,
)
from src.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from src.core.i18n import resolve_notification_lang_agent
from src.core.images import process_cover, process_logo
from src.core.rbac.baseline import PLATFORM_ROLE_NAMES
from src.core.security import hash_password
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)

_EMAIL_TAKEN = "This email already has an agent account."

# Structure F. Seats bill from the 4th; the caps are hard PRODUCT limits
# per plan (not prices — they stay in code). An unconverted agency (trial)
# is capped at the 3 included seats of the future base plan.
# SEAT_PRICES_EUR feeds the DEPRECATED informational seat_price_eur column
# only — la vérité tarifaire vit chez Paddle (PRICE_IDS).
SEAT_PRICES_EUR = {SubscriptionPlan.CABINET.value: 35, SubscriptionPlan.AGENCE.value: 25}
# Grid nidria.com/#tarifs (2026-07). THE single truth for included seats —
# the former agency.seats_included column is DROPPED (a per-row copy of a
# plan property was a second truth waiting to diverge). Semantics of the
# MAX dicts: a CONVERTED plan absent from them (sur_mesure) = NO cap
# (unlimited, gates skip); no plan at all = the trial limits.
SEATS_INCLUDED_BY_PLAN = {SubscriptionPlan.CABINET.value: 3, SubscriptionPlan.AGENCE.value: 6}
SEATS_MAX_BY_PLAN = {SubscriptionPlan.CABINET.value: 5, SubscriptionPlan.AGENCE.value: 10}
TRIAL_SEAT_LIMIT = 3
TRIAL_SEATS_INCLUDED = 3
# Providers WITH access (external agents, active + invited). The directory
# (external_contact, no login) costs nothing. Phase 1: free up to the CAP,
# blocked at the cap (409 pointing to sur-mesure); billing past the
# included tier (5 EUR/month) is PHASE 2.
PROVIDERS_INCLUDED_BY_PLAN = {SubscriptionPlan.CABINET.value: 10, SubscriptionPlan.AGENCE.value: 15}
PROVIDERS_MAX_BY_PLAN = {SubscriptionPlan.CABINET.value: 15, SubscriptionPlan.AGENCE.value: 25}
TRIAL_PROVIDER_LIMIT = 10
TRIAL_PROVIDERS_INCLUDED = 10


def seats_max_for(plan: str | None) -> int | None:
    """None = unlimited (a converted plan absent from the MAX dict:
    sur_mesure); no plan = trial limit."""
    if plan == SubscriptionPlan.SUR_MESURE.value:
        return None
    return SEATS_MAX_BY_PLAN.get(plan or "", TRIAL_SEAT_LIMIT)


def providers_max_for(plan: str | None) -> int | None:
    if plan == SubscriptionPlan.SUR_MESURE.value:
        return None
    return PROVIDERS_MAX_BY_PLAN.get(plan or "", TRIAL_PROVIDER_LIMIT)


# Referral codes: no ambiguous glyphs (0/O, 1/I/L) — typed by humans.
_REFERRAL_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _generate_referral_code() -> str:
    return "NID-" + "".join(secrets.choice(_REFERRAL_ALPHABET) for _ in range(6))


def _slugify(name: str) -> str:
    """Derive a URL-safe slug from an agency name (ASCII, lower, hyphen)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:100].strip("-")


def onboarding_gestures(
    *,
    journey_at: datetime | None,
    premier_dossier: datetime | None,
    viewed: datetime | None,
) -> list[OnboardingStepState]:
    """The 3 activation gestures from the RESOLVED signals — the SINGLE
    derivation, reused by GET /agencies/me/onboarding (self) and the superadmin
    adoption dashboard (batched). `journey_at` is already 'premier_parcours OR
    first non-demo template'; open_case falls back on the demo consultation."""
    open_at = premier_dossier or viewed
    return [
        OnboardingStepState(key="create_journey", done=journey_at is not None, done_at=journey_at),
        OnboardingStepState(key="open_case", done=open_at is not None, done_at=open_at),
        OnboardingStepState(key="view_as_client", done=viewed is not None, done_at=viewed),
    ]


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

    @staticmethod
    def _validate_sectors(sectors: list[str] | None) -> list[str]:
        """Deduplicated, each value in AgencySector, else 422 named. None or
        [] → [] (neutral). INERT: the returned list is stored, never read."""
        if not sectors:
            return []
        seen: list[str] = []
        valid = {s.value for s in AgencySector}
        for raw in sectors:
            if raw not in valid:
                raise ValidationError(
                    f"Unknown agency sector {raw!r}.",
                    code="agency.sector_invalid",
                    params={"sector": raw, "allowed": sorted(valid)},
                )
            if raw not in seen:
                seen.append(raw)
        return seen

    async def create_agency(self, superadmin: Agent, payload: AgencyCreateRequest) -> AgencyCreated:
        """Create an agency + its first admin ATOMICALLY, then stage one
        activation email. PLATFORM-scoped (superadmin only); introduces NO
        cross-agency access — the new admin, not the superadmin, will work
        the agency. The superadmin still holds only agency.create.
        """
        slug = (payload.slug or _slugify(payload.name)).strip("-")
        if not slug:
            raise ValidationError("Could not derive a slug from the name; provide one explicitly.")
        # Superadmin creation: at least one sector is mandatory (the
        # operator knows the agency's business). Self-signup does NOT go
        # through here — it defers the choice via the onboarding flag.
        if not payload.sectors:
            raise ValidationError(
                "At least one sector is required.", code="agency.sectors_required"
            )
        if await self.repo.get_agency_by_slug(slug) is not None:
            raise ConflictError(f"Agency slug '{slug}' is already taken.")
        # One human = one agent account at MVP (agent.email is table-unique):
        # refuse rather than silently re-attach an agent of another agency.
        if await self.repo.get_agent_by_email(payload.admin_email) is not None:
            raise ConflictError(_EMAIL_TAKEN)
        if payload.founding_free_seats > 0 and not payload.is_founding:
            raise ValidationError(
                "Free seats are reserved for founding agencies.",
                code="subscription.founding_seats_invalid",
            )
        agency, admin, admin_role = await self._create_agency_core(
            name=payload.name,
            slug=slug,
            default_language=payload.default_language,
            admin_email=payload.admin_email,
            admin_first_name=payload.admin_first_name,
            admin_last_name=payload.admin_last_name,
            # Throwaway: never used. The admin sets their own password via the
            # reset link below (same onboarding as the prod-seed admins).
            password_hash=hash_password(secrets.token_urlsafe(32)),
            referral_code=payload.referral_code,
            is_founding=payload.is_founding,
            founding_free_seats=payload.founding_free_seats,
            sectors=payload.sectors,
            sectors_onboarding_required=False,
            event_actor_id=superadmin.id,
        )
        # Reuse the password-reset machinery: create_reset_link only STAGES
        # the token (no commit), so agency + admin + token land in ONE
        # transaction — rollback if anything above failed. Onboarding is an
        # INVITATION: 24h window, not the 60-minute forgot-password one.
        settings = get_settings()
        reset_link = AuthManager(self.db).create_reset_link(
            admin.id, Audience.AGENT, expires_minutes=settings.onboarding_link_expires_minutes
        )
        await self._finalize_agency_creation(agency, admin)

        content = password_reset_email(
            reset_link,
            settings.onboarding_link_expires_minutes,
            resolve_notification_lang_agent(agency.default_language),
        )
        email = PendingEmail(
            to=payload.admin_email,
            subject=content.subject,
            text=content.text,
            html=content.html,
        )
        return AgencyCreated(
            agency=agency, admin=admin, admin_role_name=admin_role.name, email=email
        )

    async def _create_agency_core(
        self,
        *,
        name: str,
        slug: str,
        default_language: str,
        admin_email: str,
        admin_first_name: str,
        admin_last_name: str,
        password_hash: str,
        referral_code: str | None,
        is_founding: bool = False,
        founding_free_seats: int = 0,
        sectors: list[str] | None = None,
        sectors_onboarding_required: bool = False,
        event_actor_id: uuid.UUID | None = None,
    ) -> tuple[Agency, Agent, Role]:
        """THE single agency-creation writer, shared by the superadmin
        wizard and the self-serve signup: role, referral (attribution +
        own code), agency + admin rows, trial clock, adoption anchor.
        NO commit — the caller stages its extras then calls
        _finalize_agency_creation. Everything wired to creation (trial,
        referral, demo, milestones, nurture anchor) fires identically
        whatever the door."""
        admin_role = await self.repo.get_system_role("admin")
        if admin_role is None:
            raise NotFoundError("System role 'admin' is not seeded — run the RBAC baseline seed.")
        # Referral attribution: resolve the typed code BEFORE creating
        # anything — unknown code = explicit 422, never a silent drop.
        referrer: Agency | None = None
        if referral_code is not None:
            referrer = await self.repo.get_agency_by_referral_code(referral_code.strip().upper())
            if referrer is None:
                raise ValidationError("Unknown referral code.", code="referral.code_unknown")
        agency = self.repo.add_agency(name=name, slug=slug, default_language=default_language)
        # NID-16a: a fresh agency must never carry a NULL currency (its first
        # cost would hit the "set the agency currency" wall). Posed from the UI
        # language where unambiguous, else EUR — always editable in Settings.
        agency.currency = default_currency_for_language(default_language)
        agency.sectors = self._validate_sectors(sectors)
        agency.sectors_onboarding_required = sectors_onboarding_required
        # The agency's OWN shareable code (unique; regenerate on the rare
        # collision — 31^6 space, the loop is theoretical).
        code = _generate_referral_code()
        while await self.repo.get_agency_by_referral_code(code) is not None:
            code = _generate_referral_code()
        agency.referral_code = code
        if referrer is not None:
            agency.referred_by_agency_id = referrer.id
        agency.is_founding = is_founding
        agency.founding_free_seats = founding_free_seats
        await self.db.flush()  # need agency.id for the admin row
        admin = self.repo.add_agent(
            agency_id=agency.id,
            role_id=admin_role.id,
            email=admin_email,
            first_name=admin_first_name,
            last_name=admin_last_name,
            password_hash=password_hash,
            is_external=False,
        )
        await self.db.flush()  # need admin.id (reset token, event actor)
        # Usage trackers: the creation starts the trial clock and emits
        # the adoption anchor event (actor = the wizard's superadmin, or
        # the self-serve admin themselves).
        agency.trial_ends_at = datetime.now(UTC) + timedelta(days=get_settings().trial_days)
        await UsageManager(self.db).emit(
            agency_id=agency.id,
            event_type="agency.activated",
            actor_type=ActorType.AGENT,
            actor_id=event_actor_id if event_actor_id is not None else admin.id,
        )
        return agency, admin, admin_role

    async def _finalize_agency_creation(self, agency: Agency, admin: Agent) -> None:
        """Atomic commit, then the demo-case GIFT in its own transaction
        (best-effort: a seed failure must never cost an agency creation)."""
        await self.db.commit()
        await self.db.refresh(agency)
        await self.db.refresh(admin)
        try:
            await seed_demo_case(self.db, agency, admin)
        except Exception:
            await self.db.rollback()
            # Rollback expires the loaded rows — re-fetch before the
            # response serialization touches their attributes.
            await self.db.refresh(agency)
            await self.db.refresh(admin)
            logger.exception("demo case seed failed for agency %s", agency.slug)

    # --- hard delete (Groupe C, superadmin platform tool) ----------------------------

    async def delete_agency(
        self, superadmin: Agent, agency_id: uuid.UUID, payload: AgencyDeleteRequest
    ) -> AgencyDeletedResponse:
        """HARD delete an agency and everything it owns (platform
        housekeeping, NOT métier archival). Guardrails: the exact name
        must be re-typed (422), and live non-demo cases block it unless
        `force` (409). Isolation: the global expat accounts, their
        sessions and cases at OTHER agencies are never touched; the legal
        consent trace survives by design. One transaction for all rows,
        storage purged best-effort after commit, an audit row written."""
        agency = await self.repo.get_agency(agency_id)
        if agency is None:
            raise NotFoundError("Agency not found.")
        if payload.confirm_name != agency.name:
            raise ValidationError(
                "The typed name does not match the agency name.",
                code="agency.name_mismatch",
            )
        active = await self.repo.count_active_non_demo_cases(agency_id)
        if active > 0 and not payload.force:
            raise ConflictError(
                "The agency still has active client cases.",
                code="agency.has_active_cases",
                params={"count": active},
            )

        name, slug = agency.name, agency.slug
        total_cases = await self.repo.count_all_cases(agency_id)
        paths = await self.repo.storage_paths(agency_id)
        agent_ids = await self.repo.agent_ids(agency_id)

        await self.repo.purge_agency_rows(agency_id, agent_ids)
        self.db.add(
            AgencyDeletionLog(
                deleted_agency_id=agency_id,
                agency_name=name,
                agency_slug=slug,
                deleted_cases_count=total_cases,
                performed_by_agent_id=superadmin.id,
                performed_by_email=superadmin.email,
            )
        )
        await self.db.commit()

        # Storage AFTER the DB commit: a transient blob error must never
        # leave the agency half-deleted in the database. Best-effort;
        # orphaned blobs are logged, never fatal.
        for path in paths:
            try:
                await asyncio.to_thread(storage.delete, path)
            except Exception:
                logger.exception("agency %s deletion: storage blob not purged: %s", slug, path)

        return AgencyDeletedResponse(
            agency_id=agency_id, name=name, deleted_cases_count=total_cases
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
                    Agent.agency_id == agency.id,
                    Agent.is_external.is_(False),
                    Agent.deactivated_at.is_(None),  # offboarded = out of every count
                )
            )
        ).scalar_one()
        included = SEATS_INCLUDED_BY_PLAN.get(agency.plan or "", TRIAL_SEATS_INCLUDED)
        return SeatUsage(
            members=members,
            included=included,
            offered=agency.founding_free_seats,
            billed=max(0, members - included - agency.founding_free_seats),
            max=seats_max_for(agency.plan),
        )

    async def _providers_with_access(self, agency_id: uuid.UUID) -> int:
        """Providers WITH access = external Agent rows. The external flow
        pre-creates the Agent AT INVITATION (access_state 'invited' in the
        directory), so this single count IS "actives + invitées" without
        double-counting. Known wart (predates this lot, separate fix): a
        CANCELLED external invitation leaves its pre-created agent behind,
        which keeps counting — in the client's disfavor, flagged."""
        return (
            await self.db.execute(
                select(func.count(Agent.id)).where(
                    Agent.agency_id == agency_id,
                    Agent.is_external.is_(True),
                    Agent.deactivated_at.is_(None),  # offboarded provider = free slot
                )
            )
        ).scalar_one()

    async def subscription_info(self, agency: Agency) -> AgencySubscriptionInfo:
        from src.billing.billing_lock import blocking_reason

        reason = blocking_reason(agency, now=datetime.now(UTC))
        return AgencySubscriptionInfo(
            plan=agency.plan,
            billing_cycle=agency.billing_cycle,
            is_founding=agency.is_founding,
            trial_ends_at=agency.trial_ends_at,
            seats=await self.seat_usage(agency),
            providers=ProviderUsage(
                count=await self._providers_with_access(agency.id),
                included=PROVIDERS_INCLUDED_BY_PLAN.get(
                    agency.plan or "", TRIAL_PROVIDERS_INCLUDED
                ),
                max=providers_max_for(agency.plan),
            ),
            is_blocked=reason is not None,
            blocked_reason=reason,
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
        # A paddle-billed agency's plan/cycle/conversion are written by the
        # WEBHOOKS only — the manual hand is refused to keep one writer per
        # mode (the founding fields below stay OUR concepts, still editable).
        if agency.billing_mode == "paddle" and (
            payload.plan is not None
            or payload.billing_cycle is not None
            or payload.converted_at is not None
        ):
            raise ConflictError(
                "This agency's subscription is managed by Paddle; plan, cycle "
                "and conversion cannot be edited by hand.",
                code="subscription.managed_by_paddle",
            )
        founding_seats_changed = False
        if payload.is_founding is not None:
            agency.is_founding = payload.is_founding
        if payload.founding_free_seats is not None:
            if payload.founding_free_seats > 0 and not agency.is_founding:
                raise ValidationError(
                    "Free seats are reserved for founding agencies.",
                    code="subscription.founding_seats_invalid",
                )
            founding_seats_changed = payload.founding_free_seats != agency.founding_free_seats
            agency.founding_free_seats = payload.founding_free_seats
        if payload.price_locked_until is not None:
            agency.price_locked_until = payload.price_locked_until
        if payload.converted_at is not None and payload.plan is None:
            agency.converted_at = payload.converted_at
        if payload.billing_cycle is not None and payload.plan is None:
            agency.billing_cycle = payload.billing_cycle.value
        if payload.plan is not None:
            await self.apply_conversion(
                agency,
                plan=payload.plan.value,
                billing_cycle=payload.billing_cycle.value if payload.billing_cycle else None,
                converted_at=payload.converted_at,
                actor_type=ActorType.AGENT,
                actor_id=superadmin.id,
            )
        await self.db.commit()
        await self.db.refresh(agency)
        # Free seats change the DERIVED billed count → resync the Paddle
        # quantity (no-op for manual agencies). Best-effort after commit.
        if founding_seats_changed:
            await self._sync_paddle_seats(agency.id, increase=False)
        # Referral effects (best-effort, post-commit): a manual conversion
        # grants like a Paddle one — recompute the referrer's discount,
        # notify them, and activate the converted agency's dormant credits.
        if payload.plan is not None:
            from src.referral.referral_manager import ReferralManager

            await ReferralManager(self.db).post_conversion_effects(
                agency.id, granted=getattr(self, "last_referral_granted", False)
            )
        return await self.subscription_info(agency)

    async def apply_conversion(
        self,
        agency: Agency,
        *,
        plan: str,
        billing_cycle: str | None,
        converted_at: datetime | None,
        actor_type: ActorType,
        actor_id: uuid.UUID | None,
    ) -> None:
        """THE conversion gesture — the ONLY place `agency.converted` is
        emitted, shared by the superadmin's manual PATCH and the Paddle
        `subscription.activated` webhook, so the usage signal can never
        diverge between the two billing modes (the classify_usage_state
        lesson). NO commit: the caller owns the transaction. An explicit
        converted_at wins; otherwise the first conversion stamps now()."""
        agency.plan = plan
        # .get: sur_mesure has no seat price (a quote) — the DEPRECATED
        # informational column stays NULL for it.
        agency.seat_price_eur = SEAT_PRICES_EUR.get(plan)
        if billing_cycle is not None:
            agency.billing_cycle = billing_cycle
        if converted_at is not None:
            agency.converted_at = converted_at
        elif agency.converted_at is None:
            agency.converted_at = datetime.now(UTC)
        # Referral grant — INSIDE the single gesture, so a manually
        # converted godchild triggers exactly like a Paddle one. DB only
        # (the caller owns the transaction); the Paddle/email effects run
        # post-commit at the call sites (post_conversion_effects).
        from src.referral.referral_manager import ReferralManager

        self.last_referral_granted = await ReferralManager(self.db).grant_on_conversion(agency)
        await UsageManager(self.db).emit(
            agency_id=agency.id,
            event_type="agency.converted",
            actor_type=actor_type,
            actor_id=actor_id,
            details={
                "plan": agency.plan,
                "billing_cycle": agency.billing_cycle,
                "is_founding": agency.is_founding,
            },
        )

    async def _sync_paddle_seats(self, agency_id: uuid.UUID, *, increase: bool) -> None:
        """Best-effort seat-quantity push (paddle agencies only) — a Paddle
        hiccup must never break the member gesture that triggered it."""
        from src.billing.billing_manager import BillingManager

        try:
            await BillingManager(self.db).sync_seat_quantity(agency_id, increase=increase)
        except Exception as exc:
            if "scheduled_change" in str(exc):
                # Known case (manual test 2026-07-17): Paddle refuses
                # full_next_billing_period while a cancellation is
                # scheduled. NOT lost: the resume endpoint catches up the
                # quantity, and a sub left cancelled dies whole anyway.
                logger.error(
                    "paddle seat sync SKIPPED for agency %s: subscription has a "
                    "scheduled change (cancellation programmed) — the resume "
                    "catch-up will re-sync if the client resumes",
                    agency_id,
                )
            else:
                logger.exception("paddle seat sync failed for agency %s", agency_id)

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
                        JourneyTemplate.name != DEMO_JOURNEY_NAME,  # legacy pre-sector demo
                        JourneyTemplate.sector.is_(None),  # exclude gifted sector clones
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
        return OnboardingResponse(
            steps=onboarding_gestures(
                journey_at=journey_at,
                premier_dossier=firsts.get("premier_dossier_cree"),
                viewed=viewed_at,
            ),
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
        if payload.sectors is not None:
            agency.sectors = self._validate_sectors(payload.sectors)
            if agency.sectors:  # >= 1 sector posed → onboarding satisfied
                agency.sectors_onboarding_required = False
        if payload.settings is not None:
            agency.settings = payload.settings
        if payload.notification_prefs is not None:
            # Merge partiel cle a cle dans settings.notification_prefs.client
            # (JSONB : reassignation complete pour que SQLAlchemy voie le
            # changement). Les cles absentes gardent leur valeur/defaut.
            patch = payload.notification_prefs.model_dump(exclude_none=True)
            settings_map = dict(agency.settings or {})
            prefs = dict(settings_map.get("notification_prefs") or {})
            prefs["client"] = {**(prefs.get("client") or {}), **patch}
            settings_map["notification_prefs"] = prefs
            agency.settings = settings_map
        if payload.default_language is not None:
            agency.default_language = payload.default_language
        if payload.currency is not None:
            # The agency currency is now only the DEFAULT for a new cost line (a
            # prefill). Each line carries its OWN currency, so changing this
            # default reconverts NOTHING and is always allowed — the old
            # cost.currency_change_forbidden guard is gone.
            agency.currency = payload.currency
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

    async def deactivate_member(
        self, agent: Agent, agent_id: uuid.UUID
    ) -> MemberDeactivationResponse:
        """Offboarding (never a DELETE — the identity lives in the audit
        trail). Cuts login + live tokens (the resolve guard re-reads the
        row per request), revokes the refresh tokens, drops the member out
        of every seat/provider count, pushes the Paddle quantity DOWN
        (full_next_billing_period: the started month stays due; no-op on a
        manual agency), and returns the INVENTORY to reassign."""
        target = await self.repo.get_agent_in_agency(agent.agency_id, agent_id)
        if target is None:
            raise NotFoundError("Member not found.")
        if target.deactivated_at is not None:
            raise ConflictError(
                "This member is already deactivated.", code="member.already_deactivated"
            )
        if not target.is_external:
            # Anti-lockout, by CAPABILITY (reused from the roles domain,
            # deactivated managers already excluded from the capable list):
            # simulate the target with ZERO permissions = deactivated.
            from src.roles.roles_manager import RolesManager

            await RolesManager(self.db)._assert_agency_keeps_manager(
                agent.agency_id, reassigned_agent=(target.id, set())
            )
        now = datetime.now(UTC)
        target.deactivated_at = now
        # Live sessions die NOW: refresh revoked here, access dies at the
        # next request (the enforcement guard re-reads deactivated_at).
        from src.auth.auth_repository import AuthRepository

        await AuthRepository(self.db).revoke_all_active_refresh_tokens(
            Audience.AGENT.value, target.id, now
        )
        # The inventory: what the departed leaves to reassign (active only).
        owned = await self.repo.list_owned_case_ids(agent.agency_id, target.id)
        steps = await self.repo.list_responsible_active_steps(agent.agency_id, target.id)
        self._log_member_event(agent, target, "agent.deactivated")
        await self.db.commit()
        if not target.is_external:
            # Best-effort: a Paddle hiccup must never block an offboarding.
            await self._sync_paddle_seats(agent.agency_id, increase=False)
        return MemberDeactivationResponse(
            deactivated_at=now,
            owned_cases=owned,
            responsible_steps=[
                ResponsibleStepRef(case_id=case_id, progress_id=progress_id)
                for case_id, progress_id in steps
            ],
        )

    async def reactivate_member(self, agent: Agent, agent_id: uuid.UUID) -> None:
        """The symmetric gesture (support: a mistaken offboarding must not
        cost a ticket) — WITH the cap re-check, same rule as accepting an
        invitation: coming back consumes a seat/provider slot."""
        target = await self.repo.get_agent_in_agency(agent.agency_id, agent_id)
        if target is None:
            raise NotFoundError("Member not found.")
        if target.deactivated_at is None:
            raise ConflictError("This member is not deactivated.", code="member.not_deactivated")
        agency = await self.get_my_agency(agent)
        if target.is_external:
            cap = providers_max_for(agency.plan)
            if cap is not None and await self._providers_with_access(agency.id) >= cap:
                raise ConflictError(
                    f"The plan is capped at {cap} providers.",
                    code="subscription.provider_limit",
                    params={"max": cap, "plan": agency.plan},
                )
        else:
            usage = await self.seat_usage(agency)
            if usage.max is not None and usage.members >= usage.max:
                raise ConflictError(
                    f"The plan is capped at {usage.max} members.",
                    code="subscription.seat_limit",
                    params={"members": usage.members, "max": usage.max, "plan": agency.plan},
                )
        target.deactivated_at = None
        self._log_member_event(agent, target, "agent.reactivated")
        await self.db.commit()
        if not target.is_external:
            await self._sync_paddle_seats(agent.agency_id, increase=True)

    def _log_member_event(self, actor: Agent, target: Agent, event: str) -> None:
        logger.info("%s: agency=%s actor=%s target=%s", event, actor.agency_id, actor.id, target.id)

    async def list_roles(self, agent: Agent) -> list[Role]:
        return await self.repo.list_roles(agent.agency_id)

    # --- external providers (wave A) ---------------------------------------------

    async def list_external_roles(self, agent: Agent) -> list[Role]:
        return await self.repo.list_external_roles()

    async def list_external_members(self, agent: Agent) -> list[Agent]:
        return await self.repo.list_external_agents(agent.agency_id)

    async def create_external_invitation(
        self, agent: Agent, name: str, email: str, role_id: uuid.UUID
    ) -> AgentInvitation:
        """Invite a NEW provider: create the directory external_contact (name)
        AND the invitation, linked. agent_id is set on acceptance."""
        return await self._create_invitation(agent, email, role_id, external=True, name=name)

    async def invite_directory_contact(
        self, agent: Agent, contact_id: uuid.UUID, email: str, role_id: uuid.UUID
    ) -> AgentInvitation:
        """Invite an EXISTING directory contact: the invitation links THIS
        contact; on acceptance its agent_id is set. The contact id never
        changes; no assignment is repointed. 409 if it already has an account."""
        return await self._create_invitation(
            agent, email, role_id, external=True, existing_contact_id=contact_id
        )

    async def list_directory_contacts(self, agent: Agent) -> list[DirectoryContactListItem]:
        rows = await self.repo.list_directory_contacts(agent.agency_id)
        return [
            DirectoryContactListItem(
                id=r.id,
                name=r.name,
                email=r.email,
                phone=r.phone,
                type=r.type,
                agent_id=r.agent_id,
                agent_role=r.agent_role,
                # Authority = the invitation status: no agent → none; agent +
                # a PENDING invitation → invited; agent + none pending → active.
                access_state=(
                    "none"
                    if r.agent_id is None
                    else ("invited" if r.invited_at is not None else "active")
                ),
                invited_at=r.invited_at,
                used_in_steps=r.used_in_steps,
            )
            for r in rows
        ]

    async def create_directory_contact(
        self, agent: Agent, payload: DirectoryContactCreateRequest
    ) -> ExternalContact:
        """A NAMED provider in the agency directory (no login, no seat, no
        invitation): just a row. It can DESIGNATE a login later. Duplicate
        name in the agency → 409 (the partial unique)."""
        contact = self.repo.add_directory_contact(
            agency_id=agent.agency_id,
            name=payload.name,
            email=payload.email,
            phone=payload.phone,
            type=payload.type.value,
        )
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ConflictError(
                f"A directory contact named '{payload.name}' already exists.",
                code="external_contact.duplicate_name",
            ) from exc
        await self.db.commit()
        await self.db.refresh(contact)
        return contact

    # --- agent invitations -------------------------------------------------------

    async def create_invitation(
        self, agent: Agent, email: str, role_id: uuid.UUID
    ) -> AgentInvitation:
        return await self._create_invitation(agent, email, role_id, external=False)

    async def _create_invitation(
        self,
        agent: Agent,
        email: str,
        role_id: uuid.UUID,
        *,
        external: bool,
        name: str | None = None,
        existing_contact_id: uuid.UUID | None = None,
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
        # SEAT GATE: an INTERNAL invitation is blocked only at the plan's
        # hard cap (5 cabinet, 10 agence; 3 on trial). Between
        # included+offered and the cap the invitation goes THROUGH: the
        # app never blocks paid usage, it bills it. Externals never
        # consume a seat. Members are counted, not pending invitations —
        # the cap RE-CHECKS AT ACCEPTANCE (invitation-hygiene lot), so N
        # pending invitations on one slot can never overshoot the cap.
        if not external:
            agency = await self.get_my_agency(agent)
            usage = await self.seat_usage(agency)
            # usage.max is None = sur_mesure = no cap (gate skips).
            if usage.max is not None and usage.members >= usage.max:
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
        else:
            # PROVIDER GATE (grid 2026-07), the seat gate's mirror at the
            # SAME single point. Counting rule (Alexandre's, stricter than
            # seats): active external agents + PENDING external invitations
            # — a provider invitation IS an access being handed out; the
            # directory (external_contact, no login) costs nothing. Free up
            # to the cap in phase 1 (billing past the included tier is
            # phase 2); at the cap, the way out is the custom plan.
            agency = await self.get_my_agency(agent)
            providers = await self._providers_with_access(agency.id)
            cap = providers_max_for(agency.plan)
            if cap is not None and providers >= cap:
                message = (
                    f"The {agency.plan} plan is capped at {cap} providers; "
                    "contact us for a custom plan."
                    if agency.plan
                    else (
                        f"The trial is capped at {cap} providers; "
                        "converting to a plan unlocks more."
                    )
                )
                raise ConflictError(
                    message,
                    code="subscription.provider_limit",
                    params={"providers": providers, "max": cap, "plan": agency.plan},
                )

        # One human = one agent account = one agency at MVP
        # (agent.email is table-unique); refuse at creation, whichever
        # agency the existing account belongs to.
        if await self.repo.get_agent_by_email(email) is not None:
            raise ConflictError(_EMAIL_TAKEN)

        now = datetime.now(UTC)
        if await self.repo.get_pending_invitation(agent.agency_id, email, now) is not None:
            raise ConflictError("An invitation is already pending for this email.")

        # The directory external_contact this invitation designates (external
        # only): an EXISTING one (invite a named contact) or a NEW one created
        # here (invite a brand-new provider — name mandatory).
        external_contact_id: uuid.UUID | None = None
        if external:
            if existing_contact_id is not None:
                contact = await self.repo.get_directory_contact(
                    agent.agency_id, existing_contact_id
                )
                if contact is None:
                    raise NotFoundError(
                        "Directory contact not found.", code="external_contact.not_found"
                    )
                if contact.agent_id is not None:
                    raise ConflictError(
                        "This contact already has an account.",
                        code="external_contact.already_invited",
                    )
            else:
                contact = self.repo.add_directory_contact(
                    agency_id=agent.agency_id,
                    name=name or "",
                    email=email,
                    phone=None,
                    type=ExternalContactType.OTHER.value,
                )
                try:
                    await self.db.flush()
                except IntegrityError as exc:
                    await self.db.rollback()
                    raise ConflictError(
                        f"A directory contact named '{name}' already exists.",
                        code="external_contact.duplicate_name",
                    ) from exc
            # Pose agent_id AT INVITE so the directory is honest immediately
            # (access_state='invited', not a false 'none'). The Agent gets a
            # THROWAWAY password (unusable — login/forgot/reset are blocked while
            # the invitation is PENDING) and PLACEHOLDER names; accept poses the
            # real password + names and consumes the invitation. The directory
            # shows the CONTACT name, so the placeholder never surfaces.
            provider = self.repo.add_agent(
                agency_id=agent.agency_id,
                role_id=role_id,
                email=email,
                first_name=contact.name[:100],
                last_name="",
                password_hash=hash_password(secrets.token_urlsafe(32)),
                is_external=True,
            )
            await self.db.flush()
            contact.agent_id = provider.id
            external_contact_id = contact.id

        settings = get_settings()
        invitation = self.repo.add_invitation(
            agency_id=agent.agency_id,
            email=email,
            role_id=role_id,
            token=secrets.token_urlsafe(24),
            expires_at=now + timedelta(days=settings.agent_invitation_expires_days),
            invited_by_agent_id=agent.id,
            external_contact_id=external_contact_id,
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
        content = agent_invitation_email(
            agency.name,
            link,
            settings.agent_invitation_expires_days,
            resolve_notification_lang_agent(agency.default_language),
        )
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
        # Purge the PRE-CREATED provider agent (external flow): cancelled =
        # the access is withdrawn — the phantom must stop counting in the
        # provider gate and the directory returns to 'none' (re-invitable).
        # Safe: a PENDING invitation's pre-agent was never accepted (an
        # accepted one is not cancellable), its password is a throwaway.
        if invitation.external_contact_id is not None:
            contact = await self.db.get(ExternalContact, invitation.external_contact_id)
            if contact is not None and contact.agent_id is not None:
                phantom = await self.db.get(Agent, contact.agent_id)
                contact.agent_id = None
                if phantom is not None:
                    await self.db.delete(phantom)
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

        role = await self.repo.get_role(invitation.role_id)
        # The Agent PRE-CREATED at invite (new external flow): the invitation's
        # contact already designates it. Its email is taken by ITSELF, so we
        # ACTIVATE (real password + names) — never create nor 409.
        contact = (
            await self.db.get(ExternalContact, invitation.external_contact_id)
            if invitation.external_contact_id is not None
            else None
        )
        pre_agent = (
            await self.db.get(Agent, contact.agent_id)
            if contact is not None and contact.agent_id is not None
            else None
        )
        # CAP RE-CHECK AT ACCEPTANCE (invitation-hygiene lot): the invitation
        # gate is necessary but not sufficient — N invitations can be pending
        # on one free slot, and the self-serve billing era ended the old
        # "manual-billing tolerance". 409 to the ACCEPTANT; the invitation
        # STAYS PENDING: a seat can free up, or the agency upgrades, and the
        # same link works again.
        agency = await self.db.get(Agency, invitation.agency_id)
        assert agency is not None
        capacity_error = ConflictError(
            "This invitation cannot be accepted right now: the agency has "
            "reached its plan capacity. The invitation stays valid — retry "
            "once a seat frees up or the plan is upgraded.",
            code="invitation.capacity_reached",
            params={"plan": agency.plan},
        )
        if role is not None and role.is_external:
            cap = providers_max_for(agency.plan)
            count = await self._providers_with_access(agency.id)
            # The PRE-CREATED agent already counts itself; a legacy external
            # (no pre-creation) adds one on acceptance.
            after = count if pre_agent is not None else count + 1
            if cap is not None and after > cap:
                capacity_error.params["max"] = cap
                raise capacity_error
        else:
            usage = await self.seat_usage(agency)
            if usage.max is not None and usage.members >= usage.max:
                capacity_error.params["max"] = usage.max
                raise capacity_error
        if pre_agent is not None:
            pre_agent.password_hash = hash_password(password)
            pre_agent.first_name = first_name
            pre_agent.last_name = last_name
            agent = pre_agent
        else:
            # Legacy external (invite before this fix → no pre-created agent) OR
            # internal: create the agent. Re-check email-taken (a race between
            # invite and accept).
            if await self.repo.get_agent_by_email(invitation.email) is not None:
                raise ConflictError(_EMAIL_TAKEN)
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
            if role and role.is_external and contact is not None:
                contact.agent_id = agent.id  # legacy: contact linked, no agent yet
            elif role and role.is_external:
                # Fully-legacy external (no linked contact): create one; savepoint
                # so a duplicate name never breaks the account creation.
                try:
                    async with self.db.begin_nested():
                        self.db.add(
                            ExternalContact(
                                agency_id=agent.agency_id,
                                case_id=None,
                                name=f"{first_name} {last_name}".strip(),
                                agent_id=agent.id,
                                type=ExternalContactType.OTHER.value,
                            )
                        )
                        await self.db.flush()
                except IntegrityError:
                    pass  # duplicate directory name — account works, entry deferred

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
        # A new INTERNAL member can cross the included-seats threshold: push
        # the derived quantity to Paddle (no-op for manual agencies; external
        # providers never consume a seat). Best-effort, after commit.
        if not (role and role.is_external):
            await self._sync_paddle_seats(invitation.agency_id, increase=True)
        return pair
