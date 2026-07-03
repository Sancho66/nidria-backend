import asyncio
import uuid

from fastapi import UploadFile
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.case_person import CasePerson
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.step_case_requirement import StepCaseRequirement
from src.agencies.agencies_manager import AgenciesManager
from src.cases.case_fields import COLLECTABLE_CASE_FIELDS
from src.cases.cases_schema import (
    CaseUpdateRequest,
    CustomFieldDefinitionInline,
    PersonUpdateRequest,
)
from src.core import storage
from src.core.enums import (
    ActorType,
    ResponsibleType,
    StepRequirementKind,
    StepStatus,
    StepValidatorType,
)
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.core.i18n import DEFAULT_LANG
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.custom_fields.custom_fields_validation import validate_and_merge
from src.documents.documents_manager import DocumentsManager
from src.expat.expat_repository import ExpatRepository
from src.expat.expat_schema import (
    ExpatAgencyResponse,
    ExpatCaseDetailResponse,
    ExpatCaseSummaryResponse,
    ExpatNotificationResponse,
    ExpatParticipantResponse,
    ExpatReferentResponse,
    ExpatRequirementResponse,
    ExpatResponsibleResponse,
    ExpatTimelineStepResponse,
    RequirementValueRequest,
)
from src.progress.progress_manager import ProgressManager
from src.progress.progress_repository import ProgressRepository
from src.progress.progress_schema import StepParticipantResponse, StepProgressResponse


def _displayable_responsible(step: StepProgressResponse) -> ExpatResponsibleResponse:
    # Wave C: the named responsible is resolved upstream (responsible_name
    # + responsible_is_external). ANTI-STAFFING: an internal agent's name
    # is never shown to the client ("agency"); an EXTERNAL provider's name
    # IS shown ("Me Robert handles this") — legitimate and useful.
    if step.responsible_type == ResponsibleType.AGENT.value:
        if step.responsible_is_external:
            return ExpatResponsibleResponse(type="external", name=step.responsible_name)
        return ExpatResponsibleResponse(type="agency", name=None)
    if step.responsible_type == ResponsibleType.EXPAT.value:
        return ExpatResponsibleResponse(type="you", name=None)
    if step.responsible_type == ResponsibleType.EXTERNAL.value:
        return ExpatResponsibleResponse(type="external", name=step.responsible_name)
    return ExpatResponsibleResponse(type=None, name=None)


def _displayable_participant(p: StepParticipantResponse) -> ExpatParticipantResponse:
    # Same anti-staffing as the responsible: an internal agent → "agency"
    # (no name); an external provider → its name; the client → "you".
    if p.type == ResponsibleType.AGENT.value:
        if p.is_external:
            return ExpatParticipantResponse(role=p.role, type="external", name=p.name)
        return ExpatParticipantResponse(role=p.role, type="agency", name=None)
    if p.type == ResponsibleType.EXPAT.value:
        return ExpatParticipantResponse(role=p.role, type="you", name=None)
    if p.type == ResponsibleType.EXTERNAL.value:
        return ExpatParticipantResponse(role=p.role, type="external", name=p.name)
    return ExpatParticipantResponse(role=p.role, type=None, name=None)


class ExpatPortalManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ExpatRepository(db)

    async def _get_owned_case(
        self, expat: ExpatUser, case_id: uuid.UUID
    ) -> tuple[ClientCase, Agency]:
        # Strict ownership: 404, never 403 — a foreign case's existence
        # must not be revealed.
        row = await self.repo.get_case_for_expat(expat.id, case_id)
        if row is None:
            raise NotFoundError("Case not found.")
        return row

    def _summary(
        self,
        case: ClientCase,
        agency: Agency,
        counts: dict[uuid.UUID, tuple[int, int]],
    ) -> ExpatCaseSummaryResponse:
        done, total = counts.get(case.id, (0, 0))
        return ExpatCaseSummaryResponse(
            id=case.id,
            agency=ExpatAgencyResponse(
                name=agency.name,
                id=agency.id,
                slug=agency.slug,
                has_logo=agency.logo_path is not None,
            ),
            origin_country=case.origin_country,
            dest_country=case.dest_country,
            status=case.status,
            steps_done=done,
            steps_total=total,
            created_at=case.created_at,
            updated_at=case.updated_at,
        )

    async def list_my_cases(self, expat: ExpatUser) -> list[ExpatCaseSummaryResponse]:
        rows = await self.repo.list_cases_for_expat(expat.id)
        counts = await self.repo.step_counts([case.id for case, _ in rows])
        return [self._summary(case, agency, counts) for case, agency in rows]

    async def get_my_case(
        self, expat: ExpatUser, case_id: uuid.UUID, lang: str = DEFAULT_LANG
    ) -> ExpatCaseDetailResponse:
        case, agency = await self._get_owned_case(expat, case_id)
        counts = await self.repo.step_counts([case.id])

        referent: ExpatReferentResponse | None = None
        if case.owner_agent_id is not None:
            owner = await self.repo.get_agent(case.owner_agent_id)
            if owner is not None:
                referent = ExpatReferentResponse(
                    first_name=owner.first_name,
                    last_name=owner.last_name,
                    email=owner.email,
                )

        # The agency timeline (projected statuses) re-shaped for the
        # client: names instead of ids everywhere.
        internal_timeline = await ProgressManager(self.db).timeline_for_case(case, lang)
        timeline = [
            ExpatTimelineStepResponse(
                progress_id=step.id,
                name=step.name,
                position=step.position,
                status=step.status,
                estimated_days=step.estimated_days,
                completed_at=step.completed_at,
                blocked_by=[blocking.name for blocking in step.blocked_by],
                responsible=_displayable_responsible(step),
                participants=[_displayable_participant(p) for p in step.participants],
                completion_mode=step.completion_mode,
                comment_count=step.comment_count,
                counter=step.counter,  # resolved upstream (single source)
                # Feature 2: the client always sees the step's content on
                # their own dossier.
                content_note=step.content_note,
                attachments=step.attachments,
                # "Action validée par": the client can validate when it IS
                # the step's validator and the step is active.
                can_validate=(
                    step.validated_by_type == StepValidatorType.EXPAT.value
                    and step.status == StepStatus.IN_PROGRESS.value
                ),
                requirements=[
                    ExpatRequirementResponse(
                        id=req.id,
                        kind=req.kind,
                        reference=req.reference,
                        scope=req.scope,
                        status=req.status,
                        person_label=req.person_label,  # resolved upstream (single source)
                        value=req.value,  # resolved upstream (single source)
                        document_id=req.document_id,
                        target=req.target,  # "case" → front routes to the case-req endpoint (C2)
                    )
                    # Archived custom-field requirements are not surfaced:
                    # never ask the client to fill a retired field.
                    for req in step.requirements
                    if not req.is_archived
                ],
            )
            for step in internal_timeline
        ]
        # Same active definitions the agency face embeds — so the client
        # renders a custom_field requirement identically (no divergence).
        definitions = await CustomFieldsManager(self.db).active_definitions(case.agency_id)
        return ExpatCaseDetailResponse(
            **self._summary(case, agency, counts).model_dump(),
            referent=referent,
            timeline=timeline,
            custom_field_definitions=[
                CustomFieldDefinitionInline.model_validate(d) for d in definitions
            ],
        )

    # --- requirement fulfillment (NEW WAVE 2) --------------------------------------
    #
    # The cardinal rule (expat = read-only) is pierced ONLY here, and
    # only through four server-side borders, none trusting the payload:
    #   1. the case is the client's own (get_case_for_expat → 404);
    #   2. the requirement belongs to that case (join-scoped → 404);
    #   3. the step is ACTIVE (in_progress) — otherwise read-only;
    #   4. the target person is the requirement's own materialized
    #      person (never a payload person_id), which belongs to the case.
    # The client can only fill requirements; never mark a step done,
    # never touch anything else.

    async def _resolve_writable_requirement(
        self, expat: ExpatUser, case_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> tuple[ClientCase, CaseStepRequirement]:
        case, _ = await self._get_owned_case(expat, case_id)  # border 1
        found = await self.repo.get_requirement_in_case(case.id, requirement_id)  # border 2
        if found is None:
            raise NotFoundError("Requirement not found.")
        requirement, progress = found
        if progress.status != StepStatus.IN_PROGRESS.value:  # border 3
            raise ConflictError("This step is not active; its requirements are read-only.")
        return case, requirement

    async def fulfill_value(
        self,
        expat: ExpatUser,
        case_id: uuid.UUID,
        requirement_id: uuid.UUID,
        payload: RequirementValueRequest,
    ) -> ExpatCaseDetailResponse:
        case, requirement = await self._resolve_writable_requirement(expat, case_id, requirement_id)
        if requirement.kind == StepRequirementKind.DOCUMENT.value:
            raise ValidationError("This requirement expects a document upload, not a value.")
        person = await self.repo.get_case_person(case.id, requirement.person_id)  # border 4
        if person is None:  # defensive — a materialized person can't vanish (CASCADE)
            raise NotFoundError("Requirement not found.")

        progress_mgr = ProgressManager(self.db)
        before = await progress_mgr.snapshot_active_completion(case)
        await self._write_field(case, person, requirement, payload.value)
        # The fill may complete an auto step or arm an agency_validation
        # step — recompute, commit, then send mails (best-effort).
        pending = await progress_mgr.recompute_active(case, before)
        await self.db.commit()
        await progress_mgr.send_pending(pending)
        return await self.get_my_case(expat, case_id)

    async def _write_field(
        self,
        case: ClientCase,
        person: CasePerson,
        requirement: CaseStepRequirement,
        value: object,
    ) -> None:
        """Write the value onto case_person (the single source of truth).
        base_field → type-validated via PersonUpdateRequest; custom_field
        → validated against the agency's active definitions. Null clears
        the field (requirement returns to pending)."""
        if requirement.kind == StepRequirementKind.BASE_FIELD.value:
            try:
                validated = PersonUpdateRequest.model_validate({requirement.reference: value})
            except PydanticValidationError as exc:
                raise ValidationError(f"Invalid value for {requirement.reference!r}.") from exc
            coerced = validated.model_dump(exclude_unset=True).get(requirement.reference)
            # Enums (sex, marital_status) → store their .value.
            setattr(person, requirement.reference, getattr(coerced, "value", coerced))
        else:  # custom_field
            definitions = await CustomFieldsManager(self.db).active_definitions(case.agency_id)
            person.custom_fields = validate_and_merge(
                definitions, person.custom_fields or {}, {requirement.reference: value}
            )

    # --- CASE-level requirement fulfillment (sections chantier, vague C2) -----------
    #
    # The client writes a client_case COLUMN (country/address). Same four
    # borders as the person fulfillment above, none trusting the payload —
    # and the DECISIVE one: the column written is the DECLARATION's
    # case_field, NEVER a name from the payload (which carries only `value`).

    async def _resolve_writable_case_requirement(
        self, expat: ExpatUser, case_id: uuid.UUID, case_requirement_id: uuid.UUID
    ) -> tuple[ClientCase, StepCaseRequirement]:
        case, _ = await self._get_owned_case(expat, case_id)  # border a (ownership → 404)
        found = await self.repo.get_case_requirement_in_case(case.id, case_requirement_id)
        if found is None:  # border b (declaration on a step of THIS case → 404)
            raise NotFoundError("Case requirement not found.")
        creq, progress = found
        if progress.status != StepStatus.IN_PROGRESS.value:  # border c (active → 409)
            raise ConflictError("This step is not active; its requirements are read-only.")
        return case, creq

    async def fulfill_case_value(
        self,
        expat: ExpatUser,
        case_id: uuid.UUID,
        case_requirement_id: uuid.UUID,
        payload: RequirementValueRequest,
    ) -> ExpatCaseDetailResponse:
        case, creq = await self._resolve_writable_case_requirement(
            expat, case_id, case_requirement_id
        )
        # BORDER d (critical): the column is the DECLARATION's case_field,
        # read from the server-side row — NEVER from the payload (which has
        # only `value`). The client can touch ONLY this one column; status /
        # owner_agent_id / agency_id / any undeclared column are unreachable.
        column = creq.case_field
        if column not in COLLECTABLE_CASE_FIELDS:  # belt + braces (declaration was validated)
            raise ValidationError(f"Unknown case field {column!r}.")
        # Same value validation as the agency PATCH (pattern / length); null
        # clears the field (requirement back to pending).
        try:
            validated = CaseUpdateRequest.model_validate({column: payload.value})
        except PydanticValidationError as exc:
            raise ValidationError(f"Invalid value for {column!r}.") from exc
        coerced = validated.model_dump(exclude_unset=True).get(column)

        progress_mgr = ProgressManager(self.db)
        before = await progress_mgr.snapshot_active_completion(case)  # BEFORE the write
        setattr(case, column, coerced)  # ONE column, the declared one
        pending = await progress_mgr.recompute_active(case, before)
        await self.db.commit()
        await progress_mgr.send_pending(pending)  # best-effort: a mail failure never rolls back
        return await self.get_my_case(expat, case_id)

    async def fulfill_document(
        self,
        expat: ExpatUser,
        case_id: uuid.UUID,
        requirement_id: uuid.UUID,
        file: UploadFile,
    ) -> ExpatCaseDetailResponse:
        case, requirement = await self._resolve_writable_requirement(expat, case_id, requirement_id)
        if requirement.kind != StepRequirementKind.DOCUMENT.value:
            raise ValidationError("This requirement does not expect a document.")

        # Reuse the documents path (storage + Document row + audit); it
        # commits the upload. The document is attached to the step.
        document = await DocumentsManager(self.db).upload_as_expat(
            expat, case_id, file, requirement.case_step_progress_id
        )
        # Shared core: mark provided + link + recompute (auto→DONE etc.).
        progress_mgr = ProgressManager(self.db)
        pending = await progress_mgr.fulfill_document_requirement(case, requirement, document.id)
        await self.db.commit()
        await progress_mgr.send_pending(pending)
        return await self.get_my_case(expat, case_id)

    async def download_step_attachment(
        self,
        expat: ExpatUser,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        attachment_id: uuid.UUID,
    ) -> tuple[str, bytes]:
        """Feature 2: the client downloads an agency attachment on its own
        dossier — ALWAYS allowed (the expat sees all step content on their
        case). Borders, all server-side: (1) the case is the client's own
        (404); (2) the step is a step of THAT case (404); (3) the attachment
        belongs to THAT step's template step (404) — so a progress_id from
        another step can't serve a foreign file."""
        case, _ = await self._get_owned_case(expat, case_id)  # border 1
        progress_repo = ProgressRepository(self.db)
        progress = await progress_repo.get_progress_in_case(case.id, progress_id)  # border 2
        if progress is None:
            raise NotFoundError("Case step not found.")
        attachment = await progress_repo.get_step_attachment_in_step(  # border 3
            progress.template_step_id, attachment_id
        )
        if attachment is None:
            raise NotFoundError("Attachment not found.")
        content = await asyncio.to_thread(storage.download, attachment.storage_path)
        return attachment.filename, content

    async def validate_step(
        self, expat: ExpatUser, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> ExpatCaseDetailResponse:
        """ "Action validée par" = client: the principal validates a step of
        ITS OWN dossier. Server-side borders, none trusting the client:
        (1) the case is the client's own (404); (2) the step is a step of
        THAT case (404); (3) the step IS validated by the client
        (validated_by_type='expat'), else 409 — the client cannot close a
        step the agency/provider validates. The close itself (lock, DONE,
        audit as actor EXPAT) is the shared progress core."""
        case, _ = await self._get_owned_case(expat, case_id)  # border 1
        progress_repo = ProgressRepository(self.db)
        progress = await progress_repo.get_progress_in_case(case.id, progress_id)  # border 2
        if progress is None:
            raise NotFoundError("Case step not found.")
        if progress.validated_by_type != StepValidatorType.EXPAT.value:  # border 3
            raise ConflictError("This step is not validated by the client.")
        await ProgressManager(self.db).close_step_by_validation(
            case,
            progress,
            actor_type=ActorType.EXPAT,
            actor_id=expat.id,
            completed_by_agent_id=None,  # the client is not an agent
        )
        await self.db.commit()
        return await self.get_my_case(expat, case_id)

    async def agency_logo(self, expat: ExpatUser, agency_id: uuid.UUID) -> tuple[bytes, str]:
        """The logo of an agency holding at least one of MY live cases —
        same visibility rule as its name on my dossiers (404 otherwise,
        never revealing other agencies)."""
        rows = await self.repo.list_cases_for_expat(expat.id)
        agency = next((a for _case, a in rows if a.id == agency_id), None)
        if agency is None:
            raise NotFoundError("Logo not found.")
        return AgenciesManager(self.db).logo_bytes(agency)

    async def list_notifications(
        self, expat: ExpatUser, case_id: uuid.UUID
    ) -> list[ExpatNotificationResponse]:
        case, _ = await self._get_owned_case(expat, case_id)
        reminders = await self.repo.list_in_app_notifications(case.id)
        return [
            ExpatNotificationResponse(
                id=reminder.id,
                message_body=reminder.message_body,
                sent_at=reminder.updated_at,
            )
            for reminder in reminders
        ]
