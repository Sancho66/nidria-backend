import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_note import CaseNote
from shared.models.client_case import ClientCase
from shared.models.external_contact import ExternalContact
from shared.models.family_member import FamilyMember
from src.activity.activity_manager import ActivityManager
from src.cases.case_export import build_case_pdf
from src.cases.cases_repository import CasesRepository
from src.cases.cases_schema import (
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
    ExternalContactCreateRequest,
    ExternalContactResponse,
    ExternalContactUpdateRequest,
    FamilyMemberRequest,
    FamilyMemberResponse,
    PrincipalResponse,
)
from src.core.config import get_settings
from src.core.email import send_email
from src.core.email_templates import expat_activation_email, new_case_email
from src.core.enums import ActorType
from src.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from src.core.rbac.enforcement import effective_permissions
from src.core.rbac.permissions import Permission
from src.progress.progress_manager import ProgressManager


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
            link = f"{settings.frontend_url}/expat/activate?token={invitation.token}"
            content = expat_activation_email(
                agency_name, link, settings.case_invitation_expires_days
            )
        else:
            content = new_case_email(agency_name, f"{settings.frontend_url}/expat/login")
        await asyncio.to_thread(
            send_email, payload.email, content.subject, content.text, content.html
        )
        return case

    # --- read ---------------------------------------------------------------------

    async def list_cases(
        self, agent: Agent, filters: CaseFilters, page: int, page_size: int
    ) -> CaseListResponse:
        cases, total = await self.repo.list_cases(
            agent.agency_id, filters.as_dict(), page, page_size
        )
        return CaseListResponse(
            items=[CaseListItemResponse.model_validate(case) for case in cases],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_case_detail(self, agent: Agent, case_id: uuid.UUID) -> CaseDetailResponse:
        case = await self._get_case(agent, case_id)
        principal = await self.repo.get_expat(case.principal_expat_user_id)
        assert principal is not None  # RESTRICT FK guarantees it
        include_confidential = Permission.NOTE_VIEW_CONFIDENTIAL.value in effective_permissions(
            agent
        )
        return CaseDetailResponse(
            **CaseResponse.model_validate(case).model_dump(),
            principal=PrincipalResponse(
                id=principal.id,
                first_name=principal.first_name,
                last_name=principal.last_name,
                email=principal.email,
                preferred_lang=principal.preferred_lang,
                activated=principal.activated_at is not None,
            ),
            family_members=[
                FamilyMemberResponse.model_validate(member)
                for member in await self.repo.list_family(case_id)
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

    # --- family members -----------------------------------------------------------------

    async def add_family_member(
        self, agent: Agent, case_id: uuid.UUID, payload: FamilyMemberRequest
    ) -> FamilyMember:
        case = await self._get_case(agent, case_id)
        member = self.repo.add_family_member(
            case_id=case.id, name=payload.name, relationship=payload.relationship
        )
        await self.db.flush()
        self._log(case.id, agent, "family_member.added", {"family_member_id": str(member.id)})
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def update_family_member(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        member_id: uuid.UUID,
        payload: FamilyMemberRequest,
    ) -> FamilyMember:
        case = await self._get_case(agent, case_id)
        member = await self.repo.get_family_member(case.id, member_id)
        if member is None:
            raise NotFoundError("Family member not found.")
        member.name = payload.name
        member.relationship = payload.relationship
        self._log(case.id, agent, "family_member.updated", {"family_member_id": str(member.id)})
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def delete_family_member(
        self, agent: Agent, case_id: uuid.UUID, member_id: uuid.UUID
    ) -> None:
        case = await self._get_case(agent, case_id)
        member = await self.repo.get_family_member(case.id, member_id)
        if member is None:
            raise NotFoundError("Family member not found.")
        await self.repo.delete_row(member)
        self._log(case.id, agent, "family_member.removed", {"family_member_id": str(member_id)})
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
        activity_rows = await self.repo.list_activity_chronological(case.id)
        return build_case_pdf(
            case=case, principal=principal, owner=owner, activity_rows=activity_rows
        )
