import asyncio
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_note import CaseNote
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile as ClientProfileModel
from shared.models.custom_field import CustomFieldDefinition
from shared.models.expat_user import ExpatUser
from shared.models.external_contact import ExternalContact
from shared.models.invitation import CaseInvitation
from shared.models.journey import JourneyTemplateStep
from src.activity.activity_manager import ActivityManager
from src.cases.case_export import build_case_pdf
from src.cases.cases_repository import SORTABLE_FIELD_MAP, CasesRepository
from src.cases.cases_schema import (
    BulkActionResponse,
    CaseBillingInfo,
    CaseCreateRequest,
    CaseDetailResponse,
    CaseFilters,
    CaseListItemResponse,
    CaseListResponse,
    CaseNoteCreateRequest,
    CaseNoteResponse,
    CaseNoteUpdateRequest,
    CaseResponse,
    CaseUpdateRequest,
    CustomFieldDefinitionInline,
    ExternalContactCreateRequest,
    ExternalContactResponse,
    ExternalContactUpdateRequest,
    PersonCreateRequest,
    PersonResponse,
    PersonUpdateRequest,
    PrefillSourceResponse,
)
from src.cases.client_space import client_space_state
from src.client_profiles.client_profiles_manager import profile_divergences
from src.core.config import get_settings
from src.core.email import (
    PendingEmail,
    normalize_email,
    send_email,
    send_prepared,
    sender_as_agency,
    space_link,
)
from src.core.email_templates import expat_activation_email, new_case_email
from src.core.enums import ActorType, CasePersonKind, ClientSpaceState, InvitationStatus
from src.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    ValidationError,
)
from src.core.i18n import (
    DEFAULT_LANG,
    resolve_i18n,
    resolve_notification_lang_client,
    resolve_step_name_for_notif,
)
from src.core.notification_window import record_send
from src.core.rbac.enforcement import effective_permissions
from src.core.rbac.permissions import Permission
from src.core.seats import assert_not_reader_actor
from src.costs.costs_repository import CostsRepository
from src.costs.costs_rules import case_margin, check_amount_decimals, resolve_cost_currency
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.custom_fields.custom_fields_validation import validate_and_merge, visible_values
from src.journeys.journeys_repository import JourneysRepository
from src.progress.progress_manager import ProgressManager
from src.progress.progress_repository import ProgressRepository
from src.progress.requirements_eval import is_provided
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)

# Anti-burst on the invitation resend: the SIMPLEST guard that needs no new
# column and no new table — the last invitation's `created_at` for this
# (case, email) IS the last-sent timestamp. Two clicks inside the window →
# 429, the first mail is still on its way. Also covers the creation burst
# (a case created 2 minutes ago cannot be "re-invited" already).
RESEND_COOLDOWN = timedelta(minutes=10)


class CasesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = CasesRepository(db)
        self.activity = ActivityManager(db)

    # --- helpers ---------------------------------------------------------------

    async def _get_case(self, agent: Agent, case_id: uuid.UUID) -> ClientCase:
        case = await self.repo.get_case_in_agency(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.", code="case.not_found")
        return case

    async def _validate_owner(self, agent: Agent, owner_agent_id: uuid.UUID) -> None:
        owner = await self.repo.get_agent_in_agency(agent.agency_id, owner_agent_id)
        if owner is None:
            raise ValidationError(
                "Owner must be an agent of this agency.", code="case.owner_not_in_agency"
            )
        # Lot lecteur: a reader seat is never a designated actor — the ONE
        # owner gate covers create, PATCH and the 500-case bulk alike.
        assert_not_reader_actor(owner, designation="owner")

    async def _pending_invitations(self, case_id: uuid.UUID) -> dict[str, datetime]:
        """email → furthest PENDING invitation expiry, for ONE case. Inline
        query: cases_repository is a frozen ecosystem file (same rule and
        same reason as `_account_used_elsewhere`)."""
        rows = await self.db.execute(
            select(CaseInvitation.email, func.max(CaseInvitation.expires_at))
            .where(
                CaseInvitation.case_id == case_id,
                CaseInvitation.status == InvitationStatus.PENDING.value,
            )
            .group_by(CaseInvitation.email)
        )
        return {email: expires_at for email, expires_at in rows}

    async def _resolve_client_space(
        self, cases: list[ClientCase]
    ) -> dict[uuid.UUID, ClientSpaceState | None]:
        """The PRINCIPAL's client-space state per case id — ONE query for the
        whole page, no N+1 (same batching rule as journey_name / current_step).
        `case.principal` is already selectinload-ed by the listing query."""
        if not cases:
            return {}
        rows = await self.db.execute(
            select(
                CaseInvitation.case_id,
                CaseInvitation.email,
                func.max(CaseInvitation.expires_at),
            )
            .where(
                CaseInvitation.case_id.in_([c.id for c in cases]),
                CaseInvitation.status == InvitationStatus.PENDING.value,
            )
            .group_by(CaseInvitation.case_id, CaseInvitation.email)
        )
        pending: dict[uuid.UUID, dict[str, datetime]] = {}
        for case_id, email, expires_at in rows:
            pending.setdefault(case_id, {})[email] = expires_at
        return {
            case.id: client_space_state(case.principal, pending.get(case.id, {}))[0]
            for case in cases
        }

    def _log(
        self,
        case_id: uuid.UUID,
        agent: Agent | None,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """`agent=None` = the act was NOT an agency gesture (a client asking
        for a new activation link from an expired one): the trace says SYSTEM
        rather than crediting an agent who did nothing."""
        self.activity.log_action(
            case_id=case_id,
            actor_type=ActorType.AGENT if agent is not None else ActorType.SYSTEM,
            actor_id=agent.id if agent is not None else None,
            action_type=action_type,
            details=details,
        )

    # --- create -------------------------------------------------------------------

    async def prefill_sources(self, agent: Agent, email: str) -> list[PrefillSourceResponse]:
        """The client's dossiers in MY agency (wizard prefill picker).
        RGPD: an email known only in ANOTHER agency answers the SAME
        empty list as an unknown one — zero existence leak (import rule)."""
        expat = await self.repo.get_expat_by_email(normalize_email(email))
        if expat is None:
            return []
        rows = await self.repo.list_prefill_sources(agent.agency_id, expat.id)
        return [
            PrefillSourceResponse(id=case.id, journey_name=name, created_at=case.created_at)
            for case, name in rows
        ]

    async def create_case(
        self,
        agent: Agent,
        payload: CaseCreateRequest,
        *,
        email_sink: list[PendingEmail] | None = None,
    ) -> ClientCase:
        """Create one case (the manual UI path AND the per-row import path).

        `email_sink`: when None (default, manual path) the invitation mail is
        sent inline, exactly as before. When a list is given (CRM import) the
        mail is APPENDED to it instead of sent, so the caller can dispatch it
        out of band — the import never blocks on N synchronous sends.
        """
        owner_agent_id = payload.owner_agent_id or agent.id
        if payload.owner_agent_id is not None:
            await self._validate_owner(agent, payload.owner_agent_id)

        # Link-or-create the principal by email. An EXISTING user keeps
        # their identity — the payload's names only seed a NEW row.
        expat = await self.repo.get_expat_by_email(payload.email)
        if expat is None:
            expat = self.repo.add_expat(
                first_name=payload.first_name,
                last_name=payload.last_name,
                email=payload.email,
                preferred_lang=payload.preferred_lang,
            )
            await self.db.flush()

        # Opt-in prefill: the source must be a live dossier of THIS agency
        # for THIS client (422 otherwise; demo excluded). Person data only.
        source_persons: list[CasePerson] = []
        if payload.prefill_from_case_id is not None:
            source = await self.repo.get_case_in_agency(
                agent.agency_id, payload.prefill_from_case_id
            )
            if source is None or source.is_demo or source.principal_expat_user_id != expat.id:
                raise ValidationError(
                    "prefill_from_case_id must reference a dossier of this agency "
                    "for the same client.",
                    code="case.prefill_source_invalid",
                )
            source_persons = await self.repo.list_persons(source.id)

        case = self.repo.add_case(
            agency_id=agent.agency_id,
            principal_expat_user_id=expat.id,
            owner_agent_id=owner_agent_id,
            origin_country=payload.origin_country,
            origin_street=payload.origin_street,
            origin_city=payload.origin_city,
            origin_postal_code=payload.origin_postal_code,
            dest_country=payload.dest_country,
            dest_street=payload.dest_street,
            dest_city=payload.dest_city,
            dest_postal_code=payload.dest_postal_code,
            status=payload.status.value,
            source=payload.source,
            reference=payload.reference,
            tags=payload.tags,
        )
        # Optional billed price at creation — same gate and money rules as the
        # PATCH (cost.manage, currency resolution, decimals), one code path.
        if payload.billed_amount is not None or payload.billed_currency is not None:
            billing_data: dict[str, Any] = {
                "billed_amount": payload.billed_amount,
                "billed_currency": payload.billed_currency,
            }
            await self._apply_billing(agent, case, billing_data)
        await self.db.flush()

        # The PRINCIPAL person — civil-status carrier linked to the
        # shared login identity. Exactly one per case (DB invariant);
        # created with the case, never deletable. Wave 2: the principal's
        # OPTIONAL values (civil + custom) are applied here, same
        # validation as PATCH person (an invalid custom value → 422 with
        # NOTHING committed yet, so no orphan case).
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        source_principal = next(
            (p for p in source_persons if p.kind == CasePersonKind.PRINCIPAL.value), None
        )
        # Prefill: the source's values seed the sack (archived keys ride
        # along untouched, the orphan-keys rule), then the wizard's own
        # values are validated and WIN over the copy.
        base_custom = dict(source_principal.custom_fields or {}) if source_principal else {}
        principal = self.repo.add_person(
            case_id=case.id,
            kind=CasePersonKind.PRINCIPAL.value,
            expat_user_id=expat.id,
            custom_fields=validate_and_merge(definitions, base_custom, payload.custom_fields),
        )
        if source_principal is not None:
            self._copy_civil_fields(source_principal, principal)
        self._apply_civil_fields(principal, payload)  # wizard fields WIN over the copy
        # Chantier fiches F2.1 : liaison à la FICHE d'agence + pré-remplissage
        # fill-gap des champs scope='person' (la fiche ne comble que les
        # trous — wizard et prefill dossier gagnent toujours). Snapshot posé
        # sur le dossier, doctrine inchangée.
        from src.client_profiles.client_profiles_manager import link_and_prefill_person

        await link_and_prefill_person(self.db, agent.agency_id, principal)
        from src.client_profiles.client_profiles_manager import auto_promote_person_gaps

        await auto_promote_person_gaps(self.db, agent, principal)
        # FAMILY members ride along with their data (they belong to the
        # client, not to the dossier's lifecycle).
        for member in source_persons:
            if member.kind != CasePersonKind.FAMILY.value:
                continue
            family = self.repo.add_person(
                case_id=case.id,
                kind=CasePersonKind.FAMILY.value,
                full_name=member.full_name,
                relationship=member.relationship,
                relationship_kind=member.relationship_kind,
                custom_fields=dict(member.custom_fields or {}),
            )
            self._copy_civil_fields(member, family)
        await self.db.flush()

        # Transactional journey assignment, INSIDE this single transaction
        # (apply_journey is commit-less). If anything raises, the whole POST
        # rolls back: no orphan case, no half-assigned journey.
        #
        # NB (vague F): `required_at_creation` is NO LONGER enforced here.
        # The create modal is socle-only, so a required field can't be
        # blocking at creation; it became a non-blocking completeness
        # indicator surfaced on the case detail. The principal's optional
        # values (above) are still written when the enriched POST sends them.
        # journey_template_id is OPTIONAL again (NID-24): None = "decide
        # later" — zero steps now, the detail page carries the assign CTA and
        # POST /cases/{id}/journey instantiates them when the agency decides.
        if payload.journey_template_id is not None:
            await ProgressManager(self.db).apply_journey(agent, case, payload.journey_template_id)
        # Anti-burst J1 : le dossier nait AVEC son parcours et l'invitation
        # part a l'instant — elle suffit (l'espace montrera tout a
        # l'activation). On ouvre la fenetre "steps" pour que les
        # demarrages d'etapes qui suivent n'empilent pas de mails.
        await record_send(self.db, case.id, normalize_email(payload.email), "steps")

        # The case link IS principal_expat_user_id (just set). The
        # invitation is notification + audit trail, never the linking
        # mechanism — sent for new AND existing expats.
        settings = get_settings()
        invitation = self.repo.add_case_invitation(
            case_id=case.id,
            email=payload.email,
            token=secrets.token_urlsafe(24),
            expires_at=datetime.now(UTC) + timedelta(days=settings.case_invitation_expires_days),
        )
        usage = UsageManager(self.db)
        await usage.emit_for_case(
            case, "case.created", actor_type=ActorType.AGENT, actor_id=agent.id
        )
        await usage.emit_for_case(
            case, "case.client_invited", actor_type=ActorType.AGENT, actor_id=agent.id
        )
        if expat.activated_at is not None:
            # A client whose account is ALREADY active can follow this new
            # dossier immediately: the adoption signal holds for THIS
            # agency from case creation (flagged, never a fake activation).
            await usage.emit_for_case(
                case,
                "case.client_account_activated",
                actor_type=ActorType.AGENT,
                actor_id=agent.id,
                details={"via": "existing_account"},
            )
        self._log(case.id, agent, "case.created")
        self._log(case.id, agent, "case.invitation_sent", {"email": payload.email})
        await self.db.commit()
        await self.db.refresh(case)

        agency = await self.repo.get_agency(agent.agency_id)
        agency_name = agency.name if agency else "Votre agence"
        agency_slug = agency.slug if agency else None
        # ICP multi-métier: the invite names the JOURNEY, in the client's
        # language (neutral "votre dossier" when the case has no journey).
        lang = resolve_notification_lang_client(expat.preferred_lang)
        agency_default = (agency.default_language if agency else DEFAULT_LANG) or DEFAULT_LANG
        journey_name = await self._journey_name(agent, case, lang, agency_default)
        if expat.activated_at is None:
            # The activation screen is the FIRST thing a client ever sees:
            # it must land branded (?agency=<slug>).
            link = space_link(
                settings.frontend_url, f"/space/activate/{invitation.token}", agency_slug
            )
            content = expat_activation_email(
                agency_name, link, settings.case_invitation_expires_days, journey_name, lang
            )
        else:
            content = new_case_email(
                agency_name,
                space_link(settings.frontend_url, "/space/login", agency_slug),
                journey_name,
                lang,
            )
        if email_sink is not None:
            # Deferred: the import collects, the router dispatches later.
            email_sink.append(
                PendingEmail(
                    to=payload.email,
                    subject=content.subject,
                    text=content.text,
                    html=content.html,
                )
            )
        else:
            # After commit, best-effort (_safe_send pattern): the case IS
            # written — a Resend outage must NEVER turn a created case into
            # a 503. The invitation stays re-sendable via the existing
            # action if this mail is lost.
            try:
                await asyncio.to_thread(
                    send_email, payload.email, content.subject, content.text, content.html
                )
            except Exception:
                logger.warning(
                    "case activation email failed (case %s created; never blocking)",
                    case.id,
                    exc_info=True,
                )
        return case

    # --- read ---------------------------------------------------------------------

    async def _journey_name(
        self, agent: Agent | None, case: ClientCase, lang: str, agency_default: str
    ) -> str | None:
        """The case's journey name resolved in `lang` (the invite email's
        recipient language), or None when the case has no journey (the
        mail then falls back to a neutral 'votre dossier')."""
        if case.journey_template_id is None:
            return None
        template = await JourneysRepository(self.db).get_template_in_agency(
            agent.agency_id if agent is not None else case.agency_id, case.journey_template_id
        )
        if template is None:
            return None
        return resolve_i18n(template.name_i18n, lang, agency_default, template.name)

    async def _resolve_journey_names(
        self, agent: Agent, cases: list[ClientCase], lang: str
    ) -> dict[uuid.UUID, str]:
        """Resolved journey name per case id (display only). Batched: ONE
        template query for the whole page + ONE agency query — no N+1. Cases
        without a journey are simply absent (callers default to None)."""
        template_ids = {c.journey_template_id for c in cases if c.journey_template_id is not None}
        if not template_ids:
            return {}
        templates = await JourneysRepository(self.db).get_templates_by_ids(template_ids)
        agency = await self.repo.get_agency(agent.agency_id)
        agency_default = agency.default_language if agency else DEFAULT_LANG
        names: dict[uuid.UUID, str] = {}
        for case in cases:
            template = templates.get(case.journey_template_id) if case.journey_template_id else None
            if template is not None:
                resolved = resolve_i18n(template.name_i18n, lang, agency_default, template.name)
                if resolved is not None:
                    names[case.id] = resolved
        return names

    async def _resolve_current_steps(
        self, agent: Agent, cases: list[ClientCase], lang: str
    ) -> dict[uuid.UUID, dict[str, str | None]]:
        """current_step_name/_position per case id (display only) — the
        progression-band rule (first non-validated step in journey order),
        batched exactly like journey_name: ONE progress query for the
        whole page, no N+1. Absent case → schema defaults (no journey);
        all-validated → explicit Nones."""
        with_journey = [c.id for c in cases if c.journey_template_id is not None]
        if not with_journey:
            return {}
        rows = await ProgressRepository(self.db).current_steps_for_cases(with_journey)
        agency = await self.repo.get_agency(agent.agency_id)
        agency_default = agency.default_language if agency else DEFAULT_LANG
        out: dict[uuid.UUID, dict[str, str | None]] = {}
        for case_id, (step, index, total) in rows.items():
            if step is None:
                out[case_id] = {"current_step_name": None, "current_step_position": None}
            else:
                out[case_id] = {
                    "current_step_name": resolve_i18n(
                        step.name_i18n, lang, agency_default, step.name
                    ),
                    "current_step_position": f"{index}/{total}",
                }
        return out

    async def list_cases(
        self,
        agent: Agent,
        filters: CaseFilters,
        page: int,
        page_size: int,
        sorts: list[tuple[str, str]] | None = None,
        lang: str = DEFAULT_LANG,
    ) -> CaseListResponse:
        rows, total = await self.repo.list_cases(
            agent.agency_id, filters.as_dict(), page, page_size, sorts=sorts
        )
        cases = [case for case, _urgency in rows]
        urgencies = {case.id: urgency for case, urgency in rows}
        journey_names = await self._resolve_journey_names(agent, cases, lang)
        current_steps = await self._resolve_current_steps(agent, cases, lang)
        client_spaces = await self._resolve_client_space(cases)
        return CaseListResponse(
            items=[
                CaseListItemResponse.model_validate(case).model_copy(
                    update={
                        "journey_name": journey_names.get(case.id),
                        "urgency": urgencies[case.id],
                        "client_space_state": client_spaces.get(case.id),
                        **current_steps.get(case.id, {}),
                    }
                )
                for case in cases
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_case_detail(
        self, agent: Agent, case_id: uuid.UUID, lang: str = DEFAULT_LANG
    ) -> CaseDetailResponse:
        case = await self._get_case(agent, case_id)
        include_confidential = Permission.NOTE_VIEW_CONFIDENTIAL.value in effective_permissions(
            agent
        )
        persons = await self.repo.list_persons(case_id)
        pending_until = await self._pending_invitations(case_id)
        # Chantier fiches F2.2 : une requête pour toutes les fiches liées —
        # la divergence se calcule à la lecture, fiche en main.
        profile_ids = [p.client_profile_id for p in persons if p.client_profile_id]
        profiles_by_id: dict[uuid.UUID, ClientProfileModel] = {}
        if profile_ids:
            rows = await self.db.execute(
                select(ClientProfileModel).where(ClientProfileModel.id.in_(profile_ids))
            )
            profiles_by_id = {row.id: row for row in rows.scalars()}
        principal_person = next(p for p in persons if p.kind == CasePersonKind.PRINCIPAL.value)
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        journey_names = await self._resolve_journey_names(agent, [case], lang)
        current = (await self._resolve_current_steps(agent, [case], lang)).get(case.id, {})
        # Billing block (cost.view only): the margin needs the REAL cost lines.
        real_costs: list[tuple[Any, str]] = []
        if Permission.COST_VIEW.value in effective_permissions(agent):
            lines = await CostsRepository(self.db).list_for_case(case.id)
            real_costs = [(line.amount, line.currency) for line in lines if line.amount is not None]
        return CaseDetailResponse(
            billing=self._billing_block(agent, case, real_costs),
            **CaseResponse.model_validate(case).model_dump(),
            journey_name=journey_names.get(case.id),
            current_step_name=current.get("current_step_name"),
            current_step_position=current.get("current_step_position"),
            persons=[
                self._person_response(
                    p,
                    definitions,
                    pending_until,
                    profiles_by_id.get(p.client_profile_id) if p.client_profile_id else None,
                )
                for p in persons
            ],
            principal_person_id=principal_person.id,
            custom_field_definitions=[
                CustomFieldDefinitionInline.model_validate(d) for d in definitions
            ],
            external_contacts=[
                ExternalContactResponse.model_validate(contact)
                for contact in await self.repo.list_external_contacts(case_id)
            ],
            notes=[
                CaseNoteResponse.model_validate(note)
                for note in await self.repo.list_notes(case_id, include_confidential)
            ],
            progress=await ProgressManager(self.db).timeline_for_case(
                case, lang, viewer_agent_id=agent.id
            ),
        )

    # --- billing (the price the agency bills the dossier) ---------------------------

    async def _apply_billing(
        self, agent: Agent, case: ClientCase, data: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Apply billed_amount/billed_currency from an exclude_unset payload
        dict (keys POPPED — the generic setattr loop must never see them).
        cost.manage gates the write (same financial intimacy as the costs);
        same money rules as the costs, reused: line/agency currency
        resolution (409 cost.currency_required) + per-currency decimals.
        billed_amount=null clears the price (both fields); billed_currency
        alone re-denominates an existing price. Returns old/new changes for
        the activity log."""
        amount_present = "billed_amount" in data
        currency_present = "billed_currency" in data
        amount = data.pop("billed_amount", None)
        currency = data.pop("billed_currency", None)
        if not amount_present and not currency_present:
            return {}
        if Permission.COST_MANAGE.value not in effective_permissions(agent):
            raise ForbiddenError("Missing permission: cost.manage.")
        new_amount = amount if amount_present else case.billed_amount
        if new_amount is None:
            # Clearing (or never set): a currency without an amount is
            # meaningless — refuse rather than store half a price.
            if currency is not None:
                raise ValidationError(
                    "A billed currency without a billed amount is meaningless.",
                    code="case.billed_currency_without_amount",
                )
            new_currency = None
        else:
            requested = currency if currency is not None else case.billed_currency
            new_currency = await resolve_cost_currency(self.db, agent.agency_id, requested)
            # An ENTERED amount is checked raw (same discipline as the cost
            # lines); a STORED one being re-denominated is normalized first —
            # NUMERIC(18,4) pads trailing zeros the agency never typed.
            check_amount_decimals(
                new_amount if amount_present else new_amount.normalize(), new_currency
            )
        changes: dict[str, dict[str, Any]] = {}
        if new_amount != case.billed_amount:
            changes["billed_amount"] = {
                "old": str(case.billed_amount) if case.billed_amount is not None else None,
                "new": str(new_amount) if new_amount is not None else None,
            }
            case.billed_amount = new_amount
        if new_currency != case.billed_currency:
            changes["billed_currency"] = {"old": case.billed_currency, "new": new_currency}
            case.billed_currency = new_currency
        return changes

    def _billing_block(
        self, agent: Agent, case: ClientCase, real_costs: list[tuple[Any, str]]
    ) -> CaseBillingInfo | None:
        """The cost.view-gated billing block of the agent detail: None (the
        serializer drops the KEY) without the permission; otherwise price +
        margin via the shared case_margin rule."""
        if Permission.COST_VIEW.value not in effective_permissions(agent):
            return None
        margin, reason = case_margin(case.billed_amount, case.billed_currency, real_costs)
        return CaseBillingInfo(
            billed_amount=case.billed_amount,
            billed_currency=case.billed_currency,
            margin=margin,
            margin_unavailable_reason=reason,
        )

    # --- update --------------------------------------------------------------------

    async def update_case(
        self, agent: Agent, case_id: uuid.UUID, payload: CaseUpdateRequest
    ) -> ClientCase:
        case = await self._get_case(agent, case_id)
        data = payload.model_dump(exclude_unset=True)
        # Billed price first: pops its keys (cost.manage + money rules live in
        # _apply_billing) so the generic setattr loop never touches them.
        billing_changes = await self._apply_billing(agent, case, data)
        # Sections chantier (vague C): an address/country edit can satisfy a
        # case-level step requirement → recompute active steps (auto→DONE /
        # ready-to-validate) after the write, like the person PATCH. Snapshot
        # BEFORE the write so the agency_validation mail fires once.
        progress_mgr = ProgressManager(self.db)
        before = await progress_mgr.snapshot_active_completion(case)

        if "status" in data:
            new_status = data.pop("status").value
            if new_status != case.status:
                self._log(
                    case.id,
                    agent,
                    "case.status_changed",
                    {"old": case.status, "new": new_status},
                )
                await UsageManager(self.db).emit_for_case(
                    case,
                    "case.status_changed",
                    actor_type=ActorType.AGENT,
                    actor_id=agent.id,
                )
                case.status = new_status

        if "company_profile_id" in data:
            new_company = data.pop("company_profile_id")
            if new_company is not None:
                from src.company_profiles.company_profiles_repository import (
                    CompanyProfilesRepository,
                )

                company = await CompanyProfilesRepository(self.db).get_for_agency(
                    agent.agency_id, new_company
                )
                if company is None:
                    raise NotFoundError(
                        "Company profile not found.", code="company_profile.not_found"
                    )
            if new_company != case.company_profile_id:
                self._log(
                    case.id,
                    agent,
                    "case.company_changed",
                    {
                        "old": str(case.company_profile_id) if case.company_profile_id else None,
                        "new": str(new_company) if new_company else None,
                    },
                )
                case.company_profile_id = new_company

        if "owner_agent_id" in data:
            new_owner = data.pop("owner_agent_id")
            if new_owner is not None:
                await self._validate_owner(agent, new_owner)
            if new_owner != case.owner_agent_id:
                self._log(
                    case.id,
                    agent,
                    "case.owner_changed",
                    {
                        "old": str(case.owner_agent_id) if case.owner_agent_id else None,
                        "new": str(new_owner) if new_owner else None,
                    },
                )
                case.owner_agent_id = new_owner

        changes: dict[str, dict[str, Any]] = {}
        for field, new_value in data.items():
            old_value = getattr(case, field)
            if new_value != old_value:
                changes[field] = {"old": old_value, "new": new_value}
                setattr(case, field, new_value)
        changes.update(billing_changes)
        if changes:
            self._log(case.id, agent, "case.updated", {"changes": changes})

        pending = await progress_mgr.recompute_active(case, before)
        await self.db.commit()
        await self.db.refresh(case)
        await progress_mgr.send_pending(pending)
        return case

    # --- bulk actions --------------------------------------------------------------------

    async def bulk_set_status(
        self, agent: Agent, case_ids: list[uuid.UUID], status: str
    ) -> BulkActionResponse:
        cases = await self.repo.list_by_ids(agent.agency_id, case_ids)
        affected: list[uuid.UUID] = []
        for case in cases:
            if case.status == status:
                continue  # idempotent no-op
            self._log(case.id, agent, "case.status_changed", {"old": case.status, "new": status})
            await UsageManager(self.db).emit_for_case(
                case, "case.status_changed", actor_type=ActorType.AGENT, actor_id=agent.id
            )
            case.status = status
            affected.append(case.id)
        await self.db.commit()
        return BulkActionResponse(
            action="set_status",
            examined=len(case_ids),
            affected=len(affected),
            affected_ids=affected,
        )

    async def bulk_set_owner(
        self, agent: Agent, case_ids: list[uuid.UUID], owner_agent_id: uuid.UUID | None
    ) -> BulkActionResponse:
        # Validate the owner ONCE (membership of the agency) — same gate
        # as the unit PATCH; null means unassign.
        if owner_agent_id is not None:
            await self._validate_owner(agent, owner_agent_id)
        cases = await self.repo.list_by_ids(agent.agency_id, case_ids)
        affected: list[uuid.UUID] = []
        for case in cases:
            if case.owner_agent_id == owner_agent_id:
                continue
            self._log(
                case.id,
                agent,
                "case.owner_changed",
                {
                    "old": str(case.owner_agent_id) if case.owner_agent_id else None,
                    "new": str(owner_agent_id) if owner_agent_id else None,
                },
            )
            case.owner_agent_id = owner_agent_id
            affected.append(case.id)
        await self.db.commit()
        return BulkActionResponse(
            action="set_owner",
            examined=len(case_ids),
            affected=len(affected),
            affected_ids=affected,
        )

    async def bulk_add_tags(
        self, agent: Agent, case_ids: list[uuid.UUID], tags: list[str]
    ) -> BulkActionResponse:
        cases = await self.repo.list_by_ids(agent.agency_id, case_ids)
        affected: list[uuid.UUID] = []
        for case in cases:
            missing = [t for t in tags if t not in case.tags]
            if not missing:
                continue  # all already present → no-op
            # Reassign a NEW list so SQLAlchemy flags the JSONB dirty.
            case.tags = [*case.tags, *missing]
            self._log(case.id, agent, "case.updated", {"tags_added": missing})
            affected.append(case.id)
        await self.db.commit()
        return BulkActionResponse(
            action="add_tags",
            examined=len(case_ids),
            affected=len(affected),
            affected_ids=affected,
        )

    async def bulk_remove_tags(
        self, agent: Agent, case_ids: list[uuid.UUID], tags: list[str]
    ) -> BulkActionResponse:
        cases = await self.repo.list_by_ids(agent.agency_id, case_ids)
        to_remove = set(tags)
        affected: list[uuid.UUID] = []
        for case in cases:
            present = [t for t in case.tags if t in to_remove]
            if not present:
                continue
            case.tags = [t for t in case.tags if t not in to_remove]
            self._log(case.id, agent, "case.updated", {"tags_removed": present})
            affected.append(case.id)
        await self.db.commit()
        return BulkActionResponse(
            action="remove_tags",
            examined=len(case_ids),
            affected=len(affected),
            affected_ids=affected,
        )

    async def bulk_delete(self, agent: Agent, case_ids: list[uuid.UUID]) -> BulkActionResponse:
        # list_by_ids already excludes deleted rows → re-deleting is a
        # natural no-op (the row never comes back).
        cases = await self.repo.list_by_ids(agent.agency_id, case_ids)
        now = datetime.now(UTC)
        affected: list[uuid.UUID] = []
        for case in cases:
            case.deleted_at = now
            self._log(case.id, agent, "case.deleted", {})
            affected.append(case.id)
        await self.db.commit()
        return BulkActionResponse(
            action="delete",
            examined=len(case_ids),
            affected=len(affected),
            affected_ids=affected,
        )

    # --- persons (principal + family) ---------------------------------------------------

    _CIVIL_FIELDS = (
        "passport_number",
        "date_of_birth",
        "nationality",
        "place_of_birth",
        "sex",
        "marital_status",
        "phone",
        "preferred_channels",
        "birth_name",
        "profession",
        "employer",
    )

    async def _profile_for(self, person: CasePerson) -> "ClientProfileModel | None":
        """La fiche liée pour une réponse d'ÉCRITURE person — le détail
        dossier la charge en batch ; ici, une lecture unitaire suffit et la
        réponse cesse de mentir sur differs_from_profile (diagnostic badge)."""
        if person.client_profile_id is None:
            return None
        return await self.db.get(ClientProfileModel, person.client_profile_id)

    @staticmethod
    def _person_response(
        person: CasePerson,
        active_definitions: list[CustomFieldDefinition],
        pending_until: dict[str, datetime],
        profile: "ClientProfileModel | None" = None,
    ) -> PersonResponse:
        """Homogeneous shape: PRINCIPAL resolves identity from the shared
        expat_user (full_name NULL), FAMILY carries full_name. custom_fields
        exposes only keys with an ACTIVE definition (orphans hidden).

        `pending_until` is REQUIRED (no default): a caller that forgot it
        would silently report EXPIRED on a perfectly live invitation."""
        expat = person.expat_user
        state, invitation_expires_at = client_space_state(expat, pending_until)
        return PersonResponse(
            client_space_state=state,
            activated_at=expat.activated_at if expat else None,
            invitation_expires_at=invitation_expires_at,
            id=person.id,
            kind=person.kind,
            relationship=person.relationship,
            relationship_kind=person.relationship_kind,
            full_name=person.full_name,
            expat_user_id=person.expat_user_id,
            first_name=expat.first_name if expat else None,
            last_name=expat.last_name if expat else None,
            email=expat.email if expat else None,
            preferred_lang=expat.preferred_lang if expat else None,
            activated=(expat.activated_at is not None) if expat else None,
            passport_number=person.passport_number,
            date_of_birth=person.date_of_birth,
            nationality=person.nationality,
            place_of_birth=person.place_of_birth,
            sex=person.sex,
            marital_status=person.marital_status,
            phone=person.phone,
            preferred_channels=person.preferred_channels or [],
            birth_name=person.birth_name,
            profession=person.profession,
            employer=person.employer,
            custom_fields=visible_values(active_definitions, person.custom_fields or {}),
            client_profile_id=person.client_profile_id,
            inherited_keys=list(person.inherited_keys or []),
            # Chantier fiches F2.2 : la divergence fiche↔dossier est une
            # COMPARAISON à la lecture (verdict Phase 0 — zéro marqueur,
            # zéro sync). Fiche non chargée par l'appelant → liste vide.
            differs_from_profile=(
                profile_divergences(
                    person,
                    profile,
                    [d.key for d in active_definitions if getattr(d, "scope", "case") == "person"],
                )
                if profile is not None
                else []
            ),
        )

    def _copy_civil_fields(self, source: CasePerson, target: CasePerson) -> None:
        """Prefill copy: the person's DATA only, never the row's case/
        identity anchors."""
        for field in self._CIVIL_FIELDS:
            setattr(target, field, getattr(source, field))

    def _apply_civil_fields(
        self,
        person: CasePerson,
        payload: PersonCreateRequest | PersonUpdateRequest | CaseCreateRequest,
    ) -> None:
        provided = payload.model_dump(exclude_unset=True)
        for field in self._CIVIL_FIELDS:
            if field in provided:
                value = provided[field]
                if field == "preferred_channels":
                    # A list of ContactChannel → deduplicated string values
                    # for the JSONB column (never None → an empty list).
                    seen: list[str] = []
                    for c in value or []:
                        v = c.value if hasattr(c, "value") else c
                        if v not in seen:
                            seen.append(v)
                    person.preferred_channels = seen
                    continue
                # Enums (sex, marital_status) → store their .value.
                setattr(person, field, value.value if hasattr(value, "value") else value)

    async def _account_used_elsewhere(self, expat_id: uuid.UUID, case_id: uuid.UUID) -> bool:
        """Le compte est-il lie a d'AUTRES dossiers (principal ou membre) ?
        (Requetes inline : cases_repository fait partie des 7 fichiers
        geles de l'ecosysteme — on n'y ajoute rien.)"""
        principal_elsewhere = (
            await self.db.execute(
                select(func.count()).where(
                    ClientCase.principal_expat_user_id == expat_id,
                    ClientCase.id != case_id,
                    ClientCase.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        member_elsewhere = (
            await self.db.execute(
                select(func.count()).where(
                    CasePerson.expat_user_id == expat_id, CasePerson.case_id != case_id
                )
            )
        ).scalar_one()
        return bool(principal_elsewhere or member_elsewhere)

    async def _case_email_taken(
        self, case: ClientCase, email: str, *, exclude_person_id: uuid.UUID
    ) -> bool:
        taken = (
            await self.db.execute(
                select(func.count())
                .select_from(CasePerson)
                .join(ExpatUser, ExpatUser.id == CasePerson.expat_user_id)
                .where(
                    CasePerson.case_id == case.id,
                    CasePerson.id != exclude_person_id,
                    ExpatUser.email == email,
                )
            )
        ).scalar_one()
        return bool(taken)

    async def _cancel_pending_invitations(self, case_id: uuid.UUID, email: str) -> int:
        rows = (
            (
                await self.db.execute(
                    select(CaseInvitation).where(
                        CaseInvitation.case_id == case_id,
                        CaseInvitation.email == email,
                        CaseInvitation.status == InvitationStatus.PENDING.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        for invitation in rows:
            invitation.status = InvitationStatus.CANCELLED.value
        return len(rows)

    async def _get_or_create_member_account(
        self, agent: Agent, email: str, full_name: str
    ) -> ExpatUser:
        """The member-account pivot, ONE implementation for creation AND email
        edition (Arthur): linked-or-created expat_user by email, NEVER a blind
        insert — the email is globally unique, an existing user of ANOTHER
        agency is reused (one login, every dossier in its own context).

        Language (décision 27/07): the person's OWN language when known — an
        EXISTING account keeps its preferred_lang untouched — else the
        agency's default_language seeds the NEW account (it used to be the
        system 'fr': an English agency's member got a French invite)."""
        expat = await self.repo.get_expat_by_email(email)
        if expat is None:
            agency = await self.repo.get_agency(agent.agency_id)
            first, _, last = full_name.partition(" ")
            expat = self.repo.add_expat(
                first_name=first or full_name,
                last_name=last,
                email=email,
                preferred_lang=(agency.default_language if agency else DEFAULT_LANG)
                or DEFAULT_LANG,
            )
            await self.db.flush()
        return expat

    async def add_person(
        self, agent: Agent, case_id: uuid.UUID, payload: PersonCreateRequest
    ) -> PersonResponse:
        case = await self._get_case(agent, case_id)
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        custom = validate_and_merge(definitions, {}, payload.custom_fields)
        # Optional account: an email GIVES the member a read-only login —
        # the same shared semantics as the email EDITION (Arthur), one
        # function (_get_or_create_member_account), never a copy.
        expat: ExpatUser | None = None
        if payload.email is not None:
            expat = await self._get_or_create_member_account(
                agent, payload.email, payload.full_name
            )
        person = self.repo.add_person(
            case_id=case.id,
            kind=CasePersonKind.FAMILY.value,
            full_name=payload.full_name,
            relationship=payload.relationship,
            relationship_kind=payload.relationship_kind,
            expat_user_id=expat.id if expat is not None else None,
            custom_fields=custom,
        )
        self._apply_civil_fields(person, payload)
        # Chantier fiches F2.1 : un membre AVEC compte se lie à sa fiche
        # (get-or-create) et hérite fill-gap des champs person.
        if person.expat_user_id is not None:
            from src.client_profiles.client_profiles_manager import link_and_prefill_person

            await link_and_prefill_person(self.db, agent.agency_id, person)
            from src.client_profiles.client_profiles_manager import (
                auto_promote_person_gaps,
            )

            await auto_promote_person_gaps(self.db, agent, person)
        await self.db.flush()
        # Fin du gel de composition (Nicolas, repro b) : la personne ajoutée
        # gagne ses lignes each_person sur les étapes déjà actives, dans
        # CETTE transaction — les deux chemins d'ajout (assistant de création
        # et fiche) convergent ici.
        await ProgressManager(self.db).materialize_for_person(case, person)
        self._log(case.id, agent, "person.added", {"person_id": str(person.id)})
        mail = (
            await self._prepare_member_invite(agent, case, payload.email, expat)
            if expat is not None and payload.email is not None
            else None
        )
        await self.db.commit()
        if mail is not None:
            await asyncio.to_thread(send_prepared, mail)  # best-effort, after commit
        reloaded = await self.repo.get_person(case.id, person.id)
        assert reloaded is not None
        return self._person_response(
            reloaded,
            definitions,
            await self._pending_invitations(case.id),
            await self._profile_for(reloaded),
        )

    async def _member_pending_items(
        self, case: ClientCase, expat: ExpatUser, lang: str
    ) -> list[tuple[str, int]]:
        """(resolved step name, pending count) for THIS person's own
        requirement rows — the invitation's « déjà attendu de vous » block
        (lot 27/07). The invitation is the ONLY mail a non-activated member
        ever gets (NID-23 stops relances toward a locked door), so it
        carries the reason to come in. Reads the SAME evaluation as the
        space (is_provided) — the mail can never claim a piece the space
        shows as provided. The rows exist thanks to materialize_for_person
        (same transaction on the add path). Inline queries: cases_repository
        is frozen."""
        person = (
            (
                await self.db.execute(
                    select(CasePerson).where(
                        CasePerson.case_id == case.id, CasePerson.expat_user_id == expat.id
                    )
                )
            )
            .scalars()
            .first()
        )
        if person is None:
            return []
        rows = (
            await self.db.execute(
                select(CaseStepRequirement, JourneyTemplateStep)
                .join(
                    CaseStepProgress,
                    CaseStepProgress.id == CaseStepRequirement.case_step_progress_id,
                )
                .join(
                    JourneyTemplateStep,
                    JourneyTemplateStep.id == CaseStepProgress.template_step_id,
                )
                .where(
                    CaseStepProgress.case_id == case.id,
                    CaseStepRequirement.person_id == person.id,
                )
                .order_by(JourneyTemplateStep.position)
            )
        ).all()
        ordered: list[tuple[uuid.UUID, str]] = []
        counts: dict[uuid.UUID, int] = {}
        for requirement, step in rows:
            if is_provided(requirement, person):
                continue
            if step.id not in counts:
                counts[step.id] = 0
                ordered.append(
                    (step.id, resolve_step_name_for_notif(step.name_i18n, step.name, lang))
                )
            counts[step.id] += 1
        return [(name, counts[step_id]) for step_id, name in ordered]

    async def _prepare_member_invite(
        self,
        agent: Agent | None,
        case: ClientCase,
        email: str,
        expat: ExpatUser,
        action_type: str = "case.member_invited",
    ) -> tuple[str, str, str, str, str]:
        """Persist the member's case_invitation (in the current tx) and build
        its mail — same infra as the principal: an activation link for a NEW
        account, a 'a dossier awaits you' mail for an existing (activated) one.
        The membership READ access is the case_person link; this invitation is
        notification + activation path, never the write surface (a member has
        none). Returns the (to, subject, text, html) to send AFTER commit.

        `action_type`: the RESEND logs its own verb (`case.invitation_resent`)
        — the audit trail must not read like a first invitation."""
        settings = get_settings()
        invitation = self.repo.add_case_invitation(
            case_id=case.id,
            email=email,
            token=secrets.token_urlsafe(24),
            expires_at=datetime.now(UTC) + timedelta(days=settings.case_invitation_expires_days),
        )
        self._log(case.id, agent, action_type, {"email": email})
        agency = await self.repo.get_agency(
            agent.agency_id if agent is not None else case.agency_id
        )
        agency_name = agency.name if agency else "Votre agence"
        agency_slug = agency.slug if agency else None
        lang = resolve_notification_lang_client(expat.preferred_lang)
        agency_default = (agency.default_language if agency else DEFAULT_LANG) or DEFAULT_LANG
        journey_name = await self._journey_name(agent, case, lang, agency_default)
        # « Déjà attendu de vous » : les pièces pendantes de CETTE personne,
        # par étape — le seul mail qu'un membre non activé recevra (NID-23).
        pending_items = await self._member_pending_items(case, expat, lang)
        if expat.activated_at is None:
            link = space_link(
                settings.frontend_url, f"/space/activate/{invitation.token}", agency_slug
            )
            content = expat_activation_email(
                agency_name,
                link,
                settings.case_invitation_expires_days,
                journey_name,
                lang,
                pending_items=pending_items,
            )
        else:
            content = new_case_email(
                agency_name,
                space_link(settings.frontend_url, "/space/login", agency_slug),
                journey_name,
                lang,
                pending_items=pending_items,
            )
        # L'expéditeur AFFICHÉ est l'agence : le client reconnaît son
        # interlocuteur, pas notre outil (même inversion que sur l'écran de
        # consentement et les conditions).
        return email, content.subject, content.text, content.html, sender_as_agency(agency_name)

    async def update_person(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        person_id: uuid.UUID,
        payload: PersonUpdateRequest,
    ) -> PersonResponse:
        case = await self._get_case(agent, case_id)
        person = await self.repo.get_person(case.id, person_id)
        if person is None:
            raise NotFoundError("Person not found.", code="case.person_not_found")
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        # Snapshot active-step completion BEFORE the write so the
        # recompute fires the ready-to-validate mail only on the
        # pending→met transition (idempotent).
        progress = ProgressManager(self.db)
        before = await progress.snapshot_active_completion(case)
        provided = payload.model_dump(exclude_unset=True)
        # Email (Arthur): giving an email to a person WITHOUT an account links
        # (or creates) the SAME pivot as at creation — one function, one
        # semantics, read-only access included. A person who ALREADY has an
        # access cannot have their email changed here: a silent re-link would
        # transfer the read history to another account — an access transfer
        # disguised as a field edit → 409, remove the access then re-invite.
        # Empty or identical email: a clean no-op.
        invite_mail: tuple[str, str, str, str, str] | None = None
        invitation_resent = False
        if provided.get("email"):
            new_email = provided["email"]
            if person.expat_user_id is None:
                expat = await self._get_or_create_member_account(
                    agent, new_email, person.full_name or new_email
                )
                person.expat_user_id = expat.id
                invite_mail = await self._prepare_member_invite(agent, case, new_email, expat)
            else:
                current = await self.db.get(ExpatUser, person.expat_user_id)
                if current is not None and current.email == new_email:
                    pass  # email identique : no-op propre
                elif current is not None and current.activated_at is not None:
                    # (c) ACTIVEE : l'email est une identite de COMPTE — le
                    # changement se fait par la personne (flux verifie, autre
                    # chantier). Refus clair, code dedie.
                    raise ConflictError(
                        "This person has an active account; the email change is theirs to make.",
                        code="person.email_locked",
                    )
                elif current is not None and await self._account_used_elsewhere(
                    current.id, case.id
                ):
                    # (b') compte non active mais PARTAGE avec d'autres
                    # dossiers (le link-or-create reutilise les comptes) :
                    # corriger "son" email toucherait une identite partagee.
                    raise ConflictError(
                        "This person has an active account; the email change is theirs to make.",
                        code="person.email_locked",
                    )
                else:
                    # Anti-collision MEME dossier : une autre personne du
                    # dossier porte deja cet email -> 409 explicite.
                    if await self._case_email_taken(case, new_email, exclude_person_id=person.id):
                        raise ConflictError(
                            "Another person of this case already uses this email.",
                            code="person.email_taken",
                        )
                    old_email = current.email if current is not None else None
                    # (b) invitation PENDING (le deblocage Nicolas) :
                    # l'ancienne invitation meurt (le token avec elle — le
                    # pattern re-POST du signup), la nouvelle part a la bonne
                    # adresse. (a) sans invitation pendante : ecriture simple.
                    cancelled = (
                        await self._cancel_pending_invitations(case.id, old_email)
                        if old_email
                        else 0
                    )
                    # Re-link par le MEME pivot que la creation. La collision
                    # avec un expat_user d'un AUTRE compte ne se gere PAS ici
                    # (decision GO) : le link-or-create reutilise ce compte,
                    # et l'ACTIVATION suit le pattern existant — le token de
                    # la nouvelle invitation resout le compte par SON email
                    # (un login, N dossiers, chacun dans son contexte).
                    fallback_name = person.full_name or (
                        f"{current.first_name} {current.last_name}" if current else new_email
                    )
                    expat = await self._get_or_create_member_account(
                        agent, new_email, fallback_name
                    )
                    person.expat_user_id = expat.id
                    if person.kind == CasePersonKind.PRINCIPAL.value:
                        case.principal_expat_user_id = expat.id
                    if cancelled:
                        invite_mail = await self._prepare_member_invite(
                            agent, case, new_email, expat
                        )
                        invitation_resent = True
        # full_name / relationship are FAMILY-only; the PRINCIPAL's name
        # lives on expat_user and is never set here.
        if person.kind == CasePersonKind.FAMILY.value:
            if "full_name" in provided and provided["full_name"] is not None:
                person.full_name = provided["full_name"]
            if "relationship" in provided and provided["relationship"] is not None:
                person.relationship = provided["relationship"]
            if "relationship_kind" in provided:
                person.relationship_kind = provided["relationship_kind"]
        self._apply_civil_fields(person, payload)
        # custom_fields: partial MERGE on the keys PRESENT in the payload
        # (point 1 — never a retroactive required block on absent keys).
        if "custom_fields" in provided and payload.custom_fields is not None:
            person.custom_fields = validate_and_merge(
                definitions, person.custom_fields or {}, payload.custom_fields
            )
        # AUTO-PROMOTION (chantier fiches) : les références ÉCRITES dont la
        # fiche liée n'a AUCUNE valeur montent automatiquement (tracé
        # auto=true) ; une valeur différente sur la fiche ne bouge pas (la
        # divergence reste visible).
        from src.client_profiles.client_profiles_manager import auto_promote_person_gaps

        written_refs = [f for f in self._CIVIL_FIELDS if f in provided] + list(
            (payload.custom_fields or {}) if "custom_fields" in provided else {}
        )
        await auto_promote_person_gaps(self.db, agent, person, references=written_refs)
        # Option B : la saisie (même identique) retire la mention fiche.
        from src.client_profiles.client_profiles_manager import discard_inherited_keys

        discard_inherited_keys(person, written_refs)
        self._log(case.id, agent, "person.updated", {"person_id": str(person.id)})
        # Filling a civil field can complete an auto step or make an
        # agency_validation step ready to validate — recompute now.
        pending = await progress.recompute_active(case, before)
        await self.db.commit()
        if invite_mail is not None:
            await asyncio.to_thread(send_prepared, invite_mail)  # best-effort, after commit
        await progress.send_pending(pending)
        # expire_on_commit=False + identity map: this person was first loaded
        # WITH expat_user=None, and a selectinload never overwrites an
        # already-loaded relationship — after an email link-or-create, the
        # response would deny the account it just created (client_space_state
        # null on a live invitation). Expire the relationship so the reload
        # repopulates it. Pinned by test_member_access_email.
        self.db.expire(person, ["expat_user"])
        reloaded = await self.repo.get_person(case.id, person_id)
        assert reloaded is not None
        response = self._person_response(
            reloaded,
            definitions,
            await self._pending_invitations(case.id),
            await self._profile_for(reloaded),
        )
        response.invitation_resent = invitation_resent
        return response

    async def resend_invitation(
        self, agent: Agent, case_id: uuid.UUID, person_id: uuid.UUID
    ) -> PersonResponse:
        """Re-send the client-space invitation to a person who never activated:
        the current link dies, a FRESH token + expiry go out to the SAME address.

        The hole this closes: until now the only way to re-invite was to PATCH
        the person to a DIFFERENT email (update_person), so a client whose
        14-day link had expired was unreachable — no agency lever, and no
        client one either (forgot-password stays silent on a non-activated
        account). 5 real dossiers were in that state in prod on 2026-07-24.
        """
        case = await self._get_case(agent, case_id)
        person = await self.repo.get_person(case.id, person_id)
        if person is None:
            raise NotFoundError("Person not found.", code="case.person_not_found")
        if person.expat_user_id is None:
            # A family member without an account has no space to be invited to.
            # Giving them an email (PATCH person) is what creates the access —
            # and that path already sends the first invitation.
            raise ConflictError(
                "This person has no client access; add an email first.",
                code="person.no_account",
            )
        expat = await self.db.get(ExpatUser, person.expat_user_id)
        assert expat is not None
        if expat.activated_at is not None:
            # Nothing to re-send — and re-issuing an activation token on a LIVE
            # account would be an takeover vector by mail (the same reason
            # activate_expat never resets the password of an active account).
            raise ConflictError(
                "This person's client space is already active.",
                code="invitation.already_accepted",
            )
        last_sent = (
            await self.db.execute(
                select(func.max(CaseInvitation.created_at)).where(
                    CaseInvitation.case_id == case.id, CaseInvitation.email == expat.email
                )
            )
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if last_sent is not None and now - last_sent < RESEND_COOLDOWN:
            raise TooManyRequestsError(
                "An invitation was just sent to this address; try again in a few minutes.",
                code="invitation.resend_too_soon",
            )
        # The old link dies with its token (idempotent: zero pending rows is a
        # clean no-op) so a case never carries two live activation tokens.
        await self._cancel_pending_invitations(case.id, expat.email)
        invite_mail = await self._prepare_member_invite(
            agent, case, expat.email, expat, action_type="case.invitation_resent"
        )
        await self.db.commit()
        # Best-effort after commit, the house rule: the token IS rotated, so a
        # Resend outage must not 502 an action whose DB effect already landed —
        # the state flips to PENDING and the agent can retry after the cooldown.
        try:
            await asyncio.to_thread(send_prepared, invite_mail)
        except Exception:
            logger.warning(
                "invitation resend mail failed (case %s, token rotated)", case.id, exc_info=True
            )
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        reloaded = await self.repo.get_person(case.id, person_id)
        assert reloaded is not None
        response = self._person_response(
            reloaded,
            definitions,
            await self._pending_invitations(case.id),
            await self._profile_for(reloaded),
        )
        response.invitation_resent = True
        return response

    async def rotate_client_invitation(
        self, case: ClientCase, expat: ExpatUser, email: str
    ) -> tuple[str, str, str, str, str] | None:
        """Kill the live tokens of (case, email) and mint a NEW invitation —
        the gesture behind « ce lien a expiré, recevez-en un nouveau ». Returns
        the mail to send AFTER commit, or None when the cooldown is still
        running (the caller answers the same 200 either way: a public endpoint
        must not become a send channel).

        Same cooldown window and same rotation as the agent-side resend, and
        the SAME mail builder — the client's self-service link cannot drift
        from the one an agent sends."""
        last_sent = (
            await self.db.execute(
                select(func.max(CaseInvitation.created_at)).where(
                    CaseInvitation.case_id == case.id, CaseInvitation.email == email
                )
            )
        ).scalar_one_or_none()
        if last_sent is not None and datetime.now(UTC) - last_sent < RESEND_COOLDOWN:
            return None
        await self._cancel_pending_invitations(case.id, email)
        return await self._prepare_member_invite(
            None, case, email, expat, action_type="case.invitation_resent_by_client"
        )

    async def delete_person(self, agent: Agent, case_id: uuid.UUID, person_id: uuid.UUID) -> None:
        case = await self._get_case(agent, case_id)
        person = await self.repo.get_person(case.id, person_id)
        if person is None:
            raise NotFoundError("Person not found.", code="case.person_not_found")
        if person.kind == CasePersonKind.PRINCIPAL.value:
            # The principal is the file holder — never deletable.
            raise ValidationError(
                "The principal cannot be removed from a case.", code="case.principal_not_removable"
            )
        await self.repo.delete_row(person)
        self._log(case.id, agent, "person.removed", {"person_id": str(person_id)})
        await self.db.commit()

    # --- external contacts -----------------------------------------------------------------

    async def add_external_contact(
        self, agent: Agent, case_id: uuid.UUID, payload: ExternalContactCreateRequest
    ) -> ExternalContact:
        case = await self._get_case(agent, case_id)
        # A case-scoped contact's agency is the case's agency (unambiguous) —
        # stamped so the row satisfies the NOT NULL agency_id introduced with
        # the directory scope. (These /cases/.../external-contacts routes are
        # orphaned by the front but stay open; closing them is a separate call.)
        contact = self.repo.add_external_contact(
            agency_id=case.agency_id,
            case_id=case.id,
            name=payload.name,
            email=payload.email,
            phone=payload.phone,
            type=payload.type.value,
        )
        await self.db.flush()
        self._log(
            case.id, agent, "external_contact.added", {"external_contact_id": str(contact.id)}
        )
        await self.db.commit()
        await self.db.refresh(contact)
        return contact

    async def update_external_contact(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        contact_id: uuid.UUID,
        payload: ExternalContactUpdateRequest,
    ) -> ExternalContact:
        case = await self._get_case(agent, case_id)
        contact = await self.repo.get_external_contact(case.id, contact_id)
        if contact is None:
            raise NotFoundError(
                "External contact not found.", code="case.external_contact_not_found"
            )
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(contact, field, value.value if hasattr(value, "value") else value)
        self._log(
            case.id, agent, "external_contact.updated", {"external_contact_id": str(contact.id)}
        )
        await self.db.commit()
        await self.db.refresh(contact)
        return contact

    async def delete_external_contact(
        self, agent: Agent, case_id: uuid.UUID, contact_id: uuid.UUID
    ) -> None:
        case = await self._get_case(agent, case_id)
        contact = await self.repo.get_external_contact(case.id, contact_id)
        if contact is None:
            raise NotFoundError(
                "External contact not found.", code="case.external_contact_not_found"
            )
        await self.repo.delete_row(contact)
        self._log(
            case.id, agent, "external_contact.removed", {"external_contact_id": str(contact_id)}
        )
        await self.db.commit()

    # --- notes ----------------------------------------------------------------------------------

    async def list_notes(self, agent: Agent, case_id: uuid.UUID) -> list[CaseNote]:
        case = await self._get_case(agent, case_id)
        include_confidential = Permission.NOTE_VIEW_CONFIDENTIAL.value in effective_permissions(
            agent
        )
        return await self.repo.list_notes(case.id, include_confidential)

    async def create_note(
        self, agent: Agent, case_id: uuid.UUID, payload: CaseNoteCreateRequest
    ) -> CaseNote:
        case = await self._get_case(agent, case_id)
        if payload.is_confidential and (
            Permission.NOTE_VIEW_CONFIDENTIAL.value not in effective_permissions(agent)
        ):
            # Create-confidential requires read-confidential: otherwise
            # the author's own note would vanish from their view.
            raise ForbiddenError(
                "Creating a confidential note requires the dedicated permission.",
                code="case.note_confidential_forbidden",
            )
        note = self.repo.add_note(
            case_id=case.id,
            author_agent_id=agent.id,
            body=payload.body,
            is_confidential=payload.is_confidential,
        )
        await self.db.flush()
        # Details NEVER carry the note body — the journal must not leak
        # what note.view_confidential protects.
        self._log(
            case.id,
            agent,
            "note.added",
            {"note_id": str(note.id), "is_confidential": note.is_confidential},
        )
        await self.db.commit()
        await self.db.refresh(note)
        return note

    async def _get_own_note(self, agent: Agent, case_id: uuid.UUID, note_id: uuid.UUID) -> CaseNote:
        case = await self._get_case(agent, case_id)
        note = await self.repo.get_note(case.id, note_id)
        if note is None:
            raise NotFoundError("Note not found.", code="case.note_not_found")
        if note.author_agent_id != agent.id:
            raise ForbiddenError("Only the author can modify a note.", code="case.note_not_author")
        return note

    async def update_note(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        note_id: uuid.UUID,
        payload: CaseNoteUpdateRequest,
    ) -> CaseNote:
        note = await self._get_own_note(agent, case_id, note_id)
        note.body = payload.body
        self._log(
            case_id,
            agent,
            "note.updated",
            {"note_id": str(note.id), "is_confidential": note.is_confidential},
        )
        await self.db.commit()
        await self.db.refresh(note)
        return note

    async def delete_note(self, agent: Agent, case_id: uuid.UUID, note_id: uuid.UUID) -> None:
        note = await self._get_own_note(agent, case_id, note_id)
        details = {"note_id": str(note.id), "is_confidential": note.is_confidential}
        await self.repo.delete_row(note)
        self._log(case_id, agent, "note.removed", details)
        await self.db.commit()

    # --- export -----------------------------------------------------------------------------------

    async def export_pdf(self, agent: Agent, case_id: uuid.UUID, lang: str = DEFAULT_LANG) -> bytes:
        case = await self._get_case(agent, case_id)
        principal = await self.repo.get_expat(case.principal_expat_user_id)
        assert principal is not None
        owner: Agent | None = None
        if case.owner_agent_id is not None:
            owner = await self.repo.get_agent_in_agency(agent.agency_id, case.owner_agent_id)
        persons = await self.repo.list_persons(case.id)
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        activity_rows = await self.repo.list_activity_chronological(case.id)
        agency = await self.repo.get_agency(agent.agency_id)
        agency_default = agency.default_language if agency else DEFAULT_LANG
        # Usage tracker: the export is a read — the event is the only
        # write, committed here on purpose.
        await UsageManager(self.db).emit_for_case(
            case, "case.exported_pdf", actor_type=ActorType.AGENT, actor_id=agent.id
        )
        await self.db.commit()
        return build_case_pdf(
            case=case,
            principal=principal,
            owner=owner,
            persons=persons,
            custom_field_definitions=definitions,
            activity_rows=activity_rows,
            lang=lang,
            agency_default=agency_default,
        )


# --- Multi-sort (cases list) --------------------------------------------------
#
# Field → column resolution lives in `cases_repository.SORTABLE_FIELD_MAP`
# (single source of truth, next to the SQL columns). Ported from Prism:
# `?sort_by=a,b&order=asc,desc`, paired 1-to-1, strict 422 on unknown
# field/direction or length mismatch.

# `urgency` is not a plain column (derived subquery, resolved in the repo via
# urgency_rank_expr) — allowed for sorting alongside the mapped columns.
ALLOWED_SORTABLE_FIELDS: frozenset[str] = frozenset(SORTABLE_FIELD_MAP.keys()) | {"urgency"}
_ALLOWED_SORT_DIRS: frozenset[str] = frozenset({"asc", "desc"})


def parse_sorts(sort_by: str | None, order: str | None) -> list[tuple[str, str]]:
    """Parse `?sort_by=a,b&order=asc,desc` into `[("a","asc"),("b","desc")]`.

    Both omitted/empty → `[]` (default-order branch in the repo).
    Different lengths, unknown field or unknown direction →
    `ValueError`, translated to 422 by the router."""
    fields = [f.strip() for f in (sort_by or "").split(",") if f.strip()]
    directions = [d.strip().lower() for d in (order or "").split(",") if d.strip()]
    if not fields and not directions:
        return []
    if len(fields) != len(directions):
        raise ValueError("sort_by and order must have the same number of comma-separated values")
    sorts: list[tuple[str, str]] = []
    for field, direction in zip(fields, directions, strict=True):
        if field not in ALLOWED_SORTABLE_FIELDS:
            raise ValueError(
                f"Unknown sort field {field!r} — allowed: {sorted(ALLOWED_SORTABLE_FIELDS)}"
            )
        if direction not in _ALLOWED_SORT_DIRS:
            raise ValueError(f"Unknown sort direction {direction!r} — use asc or desc")
        sorts.append((field, direction))
    return sorts
