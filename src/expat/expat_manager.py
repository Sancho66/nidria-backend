import uuid
from datetime import UTC, datetime

from fastapi import UploadFile
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.case_person import CasePerson
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from src.cases.cases_schema import CustomFieldDefinitionInline, PersonUpdateRequest
from src.core.enums import (
    RequirementStatus,
    ResponsibleType,
    StepRequirementKind,
    StepStatus,
)
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.custom_fields.custom_fields_validation import validate_and_merge
from src.documents.documents_manager import DocumentsManager
from src.expat.expat_repository import ExpatRepository
from src.expat.expat_schema import (
    ExpatAgencyResponse,
    ExpatCaseDetailResponse,
    ExpatCaseSummaryResponse,
    ExpatNotificationResponse,
    ExpatReferentResponse,
    ExpatRequirementResponse,
    ExpatResponsibleResponse,
    ExpatTimelineStepResponse,
    RequirementValueRequest,
)
from src.progress.progress_manager import ProgressManager
from src.progress.progress_schema import StepProgressResponse


def _displayable_responsible(
    step: StepProgressResponse, external_names: dict[uuid.UUID, str]
) -> ExpatResponsibleResponse:
    if step.responsible_type == ResponsibleType.AGENT.value:
        return ExpatResponsibleResponse(type="agency", name=None)
    if step.responsible_type == ResponsibleType.EXPAT.value:
        return ExpatResponsibleResponse(type="you", name=None)
    if step.responsible_type == ResponsibleType.EXTERNAL.value:
        name = (
            external_names.get(step.responsible_external_id)
            if step.responsible_external_id
            else None
        )
        return ExpatResponsibleResponse(type="external", name=name)
    return ExpatResponsibleResponse(type=None, name=None)


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
            agency=ExpatAgencyResponse(name=agency.name),
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

    async def get_my_case(self, expat: ExpatUser, case_id: uuid.UUID) -> ExpatCaseDetailResponse:
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
        internal_timeline = await ProgressManager(self.db).timeline_for_case(case)
        external_ids = [
            step.responsible_external_id
            for step in internal_timeline
            if step.responsible_external_id is not None
        ]
        external_names = await self.repo.external_contact_names(external_ids)
        timeline = [
            ExpatTimelineStepResponse(
                name=step.name,
                position=step.position,
                status=step.status,
                estimated_days=step.estimated_days,
                completed_at=step.completed_at,
                blocked_by=[blocking.name for blocking in step.blocked_by],
                responsible=_displayable_responsible(step, external_names),
                required_documents=step.required_documents,
                completion_mode=step.completion_mode,
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

        progress_mgr = ProgressManager(self.db)
        before = await progress_mgr.snapshot_active_completion(case)
        # Reuse the documents path (storage + Document row + audit); it
        # commits the upload. The document is attached to the step.
        document = await DocumentsManager(self.db).upload_as_expat(
            expat, case_id, file, requirement.case_step_progress_id
        )
        # Authoritative for document kind: mark provided + link.
        requirement.status = RequirementStatus.PROVIDED.value
        requirement.provided_at = datetime.now(UTC)
        requirement.document_id = document.id
        pending = await progress_mgr.recompute_active(case, before)
        await self.db.commit()
        await progress_mgr.send_pending(pending)
        return await self.get_my_case(expat, case_id)

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
