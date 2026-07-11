import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_note import CaseNote
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.expat_user import ExpatUser
from shared.models.external_contact import ExternalContact
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
from src.core.config import get_settings
from src.core.email import PendingEmail, normalize_email, send_email, space_link
from src.core.email_templates import expat_activation_email, new_case_email
from src.core.enums import ActorType, CasePersonKind
from src.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from src.core.i18n import DEFAULT_LANG, resolve_i18n, resolve_notification_lang_client
from src.core.rbac.enforcement import effective_permissions
from src.core.rbac.permissions import Permission
from src.costs.costs_repository import CostsRepository
from src.costs.costs_rules import case_margin, check_amount_decimals, resolve_cost_currency
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.custom_fields.custom_fields_validation import validate_and_merge, visible_values
from src.journeys.journeys_repository import JourneysRepository
from src.progress.progress_manager import ProgressManager
from src.progress.progress_repository import ProgressRepository
from src.usage.usage_manager import UsageManager


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

    def _log(
        self,
        case_id: uuid.UUID,
        agent: Agent,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.activity.log_action(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
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
        if payload.journey_template_id is not None:
            await ProgressManager(self.db).apply_journey(agent, case, payload.journey_template_id)

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
            await asyncio.to_thread(
                send_email, payload.email, content.subject, content.text, content.html
            )
        return case

    # --- read ---------------------------------------------------------------------

    async def _journey_name(
        self, agent: Agent, case: ClientCase, lang: str, agency_default: str
    ) -> str | None:
        """The case's journey name resolved in `lang` (the invite email's
        recipient language), or None when the case has no journey (the
        mail then falls back to a neutral 'votre dossier')."""
        if case.journey_template_id is None:
            return None
        template = await JourneysRepository(self.db).get_template_in_agency(
            agent.agency_id, case.journey_template_id
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
        cases, total = await self.repo.list_cases(
            agent.agency_id, filters.as_dict(), page, page_size, sorts=sorts
        )
        journey_names = await self._resolve_journey_names(agent, cases, lang)
        current_steps = await self._resolve_current_steps(agent, cases, lang)
        return CaseListResponse(
            items=[
                CaseListItemResponse.model_validate(case).model_copy(
                    update={
                        "journey_name": journey_names.get(case.id),
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
            persons=[self._person_response(p, definitions) for p in persons],
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
            progress=await ProgressManager(self.db).timeline_for_case(case, lang),
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
        "birth_name",
        "profession",
        "employer",
    )

    @staticmethod
    def _person_response(
        person: CasePerson, active_definitions: list[CustomFieldDefinition]
    ) -> PersonResponse:
        """Homogeneous shape: PRINCIPAL resolves identity from the shared
        expat_user (full_name NULL), FAMILY carries full_name. custom_fields
        exposes only keys with an ACTIVE definition (orphans hidden)."""
        expat = person.expat_user
        return PersonResponse(
            id=person.id,
            kind=person.kind,
            relationship=person.relationship,
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
            birth_name=person.birth_name,
            profession=person.profession,
            employer=person.employer,
            custom_fields=visible_values(active_definitions, person.custom_fields or {}),
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
                # Enums (sex, marital_status) → store their .value.
                setattr(person, field, value.value if hasattr(value, "value") else value)

    async def _get_or_create_member_account(self, email: str, full_name: str) -> ExpatUser:
        """The member-account pivot, ONE implementation for creation AND email
        edition (Arthur): linked-or-created expat_user by email, NEVER a blind
        insert — the email is globally unique, an existing user of ANOTHER
        agency is reused (one login, every dossier in its own context)."""
        expat = await self.repo.get_expat_by_email(email)
        if expat is None:
            first, _, last = full_name.partition(" ")
            expat = self.repo.add_expat(
                first_name=first or full_name,
                last_name=last,
                email=email,
                preferred_lang=DEFAULT_LANG,
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
            expat = await self._get_or_create_member_account(payload.email, payload.full_name)
        person = self.repo.add_person(
            case_id=case.id,
            kind=CasePersonKind.FAMILY.value,
            full_name=payload.full_name,
            relationship=payload.relationship,
            expat_user_id=expat.id if expat is not None else None,
            custom_fields=custom,
        )
        self._apply_civil_fields(person, payload)
        await self.db.flush()
        self._log(case.id, agent, "person.added", {"person_id": str(person.id)})
        mail = (
            await self._prepare_member_invite(agent, case, payload.email, expat)
            if expat is not None and payload.email is not None
            else None
        )
        await self.db.commit()
        if mail is not None:
            await asyncio.to_thread(send_email, *mail)  # best-effort, after commit
        reloaded = await self.repo.get_person(case.id, person.id)
        assert reloaded is not None
        return self._person_response(reloaded, definitions)

    async def _prepare_member_invite(
        self, agent: Agent, case: ClientCase, email: str, expat: ExpatUser
    ) -> tuple[str, str, str, str]:
        """Persist the member's case_invitation (in the current tx) and build
        its mail — same infra as the principal: an activation link for a NEW
        account, a 'a dossier awaits you' mail for an existing (activated) one.
        The membership READ access is the case_person link; this invitation is
        notification + activation path, never the write surface (a member has
        none). Returns the (to, subject, text, html) to send AFTER commit."""
        settings = get_settings()
        invitation = self.repo.add_case_invitation(
            case_id=case.id,
            email=email,
            token=secrets.token_urlsafe(24),
            expires_at=datetime.now(UTC) + timedelta(days=settings.case_invitation_expires_days),
        )
        self._log(case.id, agent, "case.member_invited", {"email": email})
        agency = await self.repo.get_agency(agent.agency_id)
        agency_name = agency.name if agency else "Votre agence"
        agency_slug = agency.slug if agency else None
        lang = resolve_notification_lang_client(expat.preferred_lang)
        agency_default = (agency.default_language if agency else DEFAULT_LANG) or DEFAULT_LANG
        journey_name = await self._journey_name(agent, case, lang, agency_default)
        if expat.activated_at is None:
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
        return email, content.subject, content.text, content.html

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
        invite_mail: tuple[str, str, str, str] | None = None
        if provided.get("email"):
            new_email = provided["email"]
            if person.expat_user_id is None:
                expat = await self._get_or_create_member_account(
                    new_email, person.full_name or new_email
                )
                person.expat_user_id = expat.id
                invite_mail = await self._prepare_member_invite(agent, case, new_email, expat)
            else:
                current = await self.db.get(ExpatUser, person.expat_user_id)
                if current is None or current.email != new_email:
                    raise ConflictError(
                        "This person already has an access; remove it, then re-invite.",
                        code="person.email_change_forbidden",
                    )
        # full_name / relationship are FAMILY-only; the PRINCIPAL's name
        # lives on expat_user and is never set here.
        if person.kind == CasePersonKind.FAMILY.value:
            if "full_name" in provided and provided["full_name"] is not None:
                person.full_name = provided["full_name"]
            if "relationship" in provided and provided["relationship"] is not None:
                person.relationship = provided["relationship"]
        self._apply_civil_fields(person, payload)
        # custom_fields: partial MERGE on the keys PRESENT in the payload
        # (point 1 — never a retroactive required block on absent keys).
        if "custom_fields" in provided and payload.custom_fields is not None:
            person.custom_fields = validate_and_merge(
                definitions, person.custom_fields or {}, payload.custom_fields
            )
        self._log(case.id, agent, "person.updated", {"person_id": str(person.id)})
        # Filling a civil field can complete an auto step or make an
        # agency_validation step ready to validate — recompute now.
        pending = await progress.recompute_active(case, before)
        await self.db.commit()
        if invite_mail is not None:
            await asyncio.to_thread(send_email, *invite_mail)  # best-effort, after commit
        await progress.send_pending(pending)
        reloaded = await self.repo.get_person(case.id, person_id)
        assert reloaded is not None
        return self._person_response(reloaded, definitions)

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

ALLOWED_SORTABLE_FIELDS: frozenset[str] = frozenset(SORTABLE_FIELD_MAP.keys())
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
