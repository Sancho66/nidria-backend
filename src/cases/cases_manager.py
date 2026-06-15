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
from shared.models.external_contact import ExternalContact
from src.activity.activity_manager import ActivityManager
from src.cases.case_export import build_case_pdf
from src.cases.cases_repository import SORTABLE_FIELD_MAP, CasesRepository
from src.cases.cases_schema import (
    BulkActionResponse,
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
)
from src.core.config import get_settings
from src.core.email import send_email
from src.core.email_templates import expat_activation_email, new_case_email
from src.core.enums import ActorType, CasePersonKind, StepRequirementKind
from src.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from src.core.rbac.enforcement import effective_permissions
from src.core.rbac.permissions import Permission
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.custom_fields.custom_fields_validation import validate_and_merge, visible_values
from src.journeys.journeys_repository import JourneysRepository
from src.progress.progress_manager import ProgressManager
from src.progress.requirements_eval import field_provided


class CasesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = CasesRepository(db)
        self.activity = ActivityManager(db)

    # --- helpers ---------------------------------------------------------------

    async def _get_case(self, agent: Agent, case_id: uuid.UUID) -> ClientCase:
        case = await self.repo.get_case_in_agency(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        return case

    async def _validate_owner(self, agent: Agent, owner_agent_id: uuid.UUID) -> None:
        owner = await self.repo.get_agent_in_agency(agent.agency_id, owner_agent_id)
        if owner is None:
            raise ValidationError("Owner must be an agent of this agency.")

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

    async def create_case(self, agent: Agent, payload: CaseCreateRequest) -> ClientCase:
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

        case = self.repo.add_case(
            agency_id=agent.agency_id,
            principal_expat_user_id=expat.id,
            owner_agent_id=owner_agent_id,
            origin_country=payload.origin_country,
            dest_country=payload.dest_country,
            status=payload.status.value,
            source=payload.source,
            tags=payload.tags,
        )
        await self.db.flush()

        # The PRINCIPAL person — civil-status carrier linked to the
        # shared login identity. Exactly one per case (DB invariant);
        # created with the case, never deletable. Wave 2: the principal's
        # OPTIONAL values (civil + custom) are applied here, same
        # validation as PATCH person (an invalid custom value → 422 with
        # NOTHING committed yet, so no orphan case).
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        principal = self.repo.add_person(
            case_id=case.id,
            kind=CasePersonKind.PRINCIPAL.value,
            expat_user_id=expat.id,
            custom_fields=validate_and_merge(definitions, {}, payload.custom_fields),
        )
        self._apply_civil_fields(principal, payload)
        await self.db.flush()

        # Wave 2 — transactional journey assignment + required-at-creation
        # enforcement, INSIDE this single transaction (apply_journey is
        # commit-less). If anything raises, the whole POST rolls back: no
        # orphan case, no half-assigned journey.
        if payload.journey_template_id is not None:
            await ProgressManager(self.db).apply_journey(agent, case, payload.journey_template_id)
            await self._enforce_required_at_creation(
                payload.journey_template_id, principal, definitions
            )

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
        self._log(case.id, agent, "case.created")
        self._log(case.id, agent, "case.invitation_sent", {"email": payload.email})
        await self.db.commit()
        await self.db.refresh(case)

        agency = await self.repo.get_agency(agent.agency_id)
        agency_name = agency.name if agency else "Votre agence"
        if expat.activated_at is None:
            link = f"{settings.frontend_url}/space/activate/{invitation.token}"
            content = expat_activation_email(
                agency_name, link, settings.case_invitation_expires_days
            )
        else:
            content = new_case_email(agency_name, f"{settings.frontend_url}/space/login")
        await asyncio.to_thread(
            send_email, payload.email, content.subject, content.text, content.html
        )
        return case

    # --- read ---------------------------------------------------------------------

    async def list_cases(
        self,
        agent: Agent,
        filters: CaseFilters,
        page: int,
        page_size: int,
        sorts: list[tuple[str, str]] | None = None,
    ) -> CaseListResponse:
        cases, total = await self.repo.list_cases(
            agent.agency_id, filters.as_dict(), page, page_size, sorts=sorts
        )
        return CaseListResponse(
            items=[CaseListItemResponse.model_validate(case) for case in cases],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_case_detail(self, agent: Agent, case_id: uuid.UUID) -> CaseDetailResponse:
        case = await self._get_case(agent, case_id)
        include_confidential = Permission.NOTE_VIEW_CONFIDENTIAL.value in effective_permissions(
            agent
        )
        persons = await self.repo.list_persons(case_id)
        principal_person = next(p for p in persons if p.kind == CasePersonKind.PRINCIPAL.value)
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        return CaseDetailResponse(
            **CaseResponse.model_validate(case).model_dump(),
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
            progress=await ProgressManager(self.db).timeline_for_case(case),
        )

    # --- update --------------------------------------------------------------------

    async def update_case(
        self, agent: Agent, case_id: uuid.UUID, payload: CaseUpdateRequest
    ) -> ClientCase:
        case = await self._get_case(agent, case_id)
        data = payload.model_dump(exclude_unset=True)

        if "status" in data:
            new_status = data.pop("status").value
            if new_status != case.status:
                self._log(
                    case.id,
                    agent,
                    "case.status_changed",
                    {"old": case.status, "new": new_status},
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
        if changes:
            self._log(case.id, agent, "case.updated", {"changes": changes})

        await self.db.commit()
        await self.db.refresh(case)
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
            custom_fields=visible_values(active_definitions, person.custom_fields or {}),
        )

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

    async def _enforce_required_at_creation(
        self,
        template_id: uuid.UUID,
        principal: CasePerson,
        definitions: list[CustomFieldDefinition],
    ) -> None:
        """If the assigned template flags fields required_at_creation, the
        principal must carry a non-empty value for each — else 422 (and,
        no commit yet, the whole creation rolls back). An ARCHIVED custom
        field is no longer demanded → skipped (it has dropped off the
        picker; blocking on it would be incoherent). Only enforced when a
        journey is assigned — a nu-case has nothing to require."""
        active_keys = {d.key for d in definitions}
        missing = []
        for field in await JourneysRepository(self.db).list_fields(template_id):
            if not field.required_at_creation:
                continue
            if (
                field.kind == StepRequirementKind.CUSTOM_FIELD.value
                and field.reference not in active_keys
            ):
                continue
            if not field_provided(principal, field.reference):
                missing.append(field.reference)
        if missing:
            raise ValidationError(f"These fields are required at creation: {sorted(missing)}.")

    async def add_person(
        self, agent: Agent, case_id: uuid.UUID, payload: PersonCreateRequest
    ) -> PersonResponse:
        case = await self._get_case(agent, case_id)
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        custom = validate_and_merge(definitions, {}, payload.custom_fields)
        person = self.repo.add_person(
            case_id=case.id,
            kind=CasePersonKind.FAMILY.value,
            full_name=payload.full_name,
            relationship=payload.relationship,
            custom_fields=custom,
        )
        self._apply_civil_fields(person, payload)
        await self.db.flush()
        self._log(case.id, agent, "person.added", {"person_id": str(person.id)})
        await self.db.commit()
        reloaded = await self.repo.get_person(case.id, person.id)
        assert reloaded is not None
        return self._person_response(reloaded, definitions)

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
            raise NotFoundError("Person not found.")
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        # Snapshot active-step completion BEFORE the write so the
        # recompute fires the ready-to-validate mail only on the
        # pending→met transition (idempotent).
        progress = ProgressManager(self.db)
        before = await progress.snapshot_active_completion(case)
        provided = payload.model_dump(exclude_unset=True)
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
        await progress.send_pending(pending)
        reloaded = await self.repo.get_person(case.id, person_id)
        assert reloaded is not None
        return self._person_response(reloaded, definitions)

    async def delete_person(self, agent: Agent, case_id: uuid.UUID, person_id: uuid.UUID) -> None:
        case = await self._get_case(agent, case_id)
        person = await self.repo.get_person(case.id, person_id)
        if person is None:
            raise NotFoundError("Person not found.")
        if person.kind == CasePersonKind.PRINCIPAL.value:
            # The principal is the file holder — never deletable.
            raise ValidationError("The principal cannot be removed from a case.")
        await self.repo.delete_row(person)
        self._log(case.id, agent, "person.removed", {"person_id": str(person_id)})
        await self.db.commit()

    # --- external contacts -----------------------------------------------------------------

    async def add_external_contact(
        self, agent: Agent, case_id: uuid.UUID, payload: ExternalContactCreateRequest
    ) -> ExternalContact:
        case = await self._get_case(agent, case_id)
        contact = self.repo.add_external_contact(
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
            raise NotFoundError("External contact not found.")
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
            raise NotFoundError("External contact not found.")
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
            raise ForbiddenError("Creating a confidential note requires the dedicated permission.")
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
            raise NotFoundError("Note not found.")
        if note.author_agent_id != agent.id:
            raise ForbiddenError("Only the author can modify a note.")
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

    async def export_pdf(self, agent: Agent, case_id: uuid.UUID) -> bytes:
        case = await self._get_case(agent, case_id)
        principal = await self.repo.get_expat(case.principal_expat_user_id)
        assert principal is not None
        owner: Agent | None = None
        if case.owner_agent_id is not None:
            owner = await self.repo.get_agent_in_agency(agent.agency_id, case.owner_agent_id)
        persons = await self.repo.list_persons(case.id)
        definitions = await CustomFieldsManager(self.db).active_definitions(agent.agency_id)
        activity_rows = await self.repo.list_activity_chronological(case.id)
        return build_case_pdf(
            case=case,
            principal=principal,
            owner=owner,
            persons=persons,
            custom_field_definitions=definitions,
            activity_rows=activity_rows,
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
