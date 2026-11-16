import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.document_template import DocumentTemplate
from src.activity.activity_manager import ActivityManager
from src.core.enums import ActorType, StepStatus
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.document_templates.document_templates_manager import DocumentTemplatesManager
from src.one_off_signatures.repository import OneOffSignaturesRepository
from src.one_off_signatures.schema import OneOffSignatureResponse
from src.progress.progress_manager import ProgressManager
from src.signatures.signatures_manager import SignaturesManager


class OneOffSignaturesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = OneOffSignaturesRepository(db)

    async def _case(self, agent: Agent, case_id: uuid.UUID) -> ClientCase:
        case = await self.repo.case(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.", code="case.not_found")
        return case

    async def _target(
        self, case_id: uuid.UUID, progress_id: uuid.UUID, person_id: uuid.UUID
    ) -> tuple[CaseStepProgress, CasePerson]:
        progress = await self.repo.progress(case_id, progress_id)
        if progress is None:
            raise NotFoundError("Case step not found.", code="progress.step_not_found")
        if progress.status == StepStatus.DONE.value:
            raise ConflictError(
                "Reopen the step before adding a signature.", code="signatures.step_completed"
            )
        person = await self.repo.person(case_id, person_id)
        if person is None:
            raise NotFoundError("Case person not found.", code="case.person_not_found")
        if person.expat_user_id is None:
            raise ValidationError(
                "The signer needs a client space.", code="signatures.client_space_required"
            )
        return progress, person

    async def _draft(
        self, agent: Agent, case_id: uuid.UUID, draft_id: uuid.UUID
    ) -> DocumentTemplate:
        await self._case(agent, case_id)
        template = await self.repo.draft(agent.agency_id, case_id, draft_id)
        if template is None:
            raise NotFoundError("One-off signature not found.", code="signatures.one_off_not_found")
        # Reuse the provider flag and exact tenant/case scope in the standard manager.
        await DocumentTemplatesManager(self.db, case_id=case_id).get(agent, draft_id)
        return template

    @staticmethod
    def _editable(template: DocumentTemplate) -> None:
        if template.submitted_at is not None:
            raise ConflictError(
                "This document has already been submitted.", code="signatures.one_off_submitted"
            )

    async def response(self, template: DocumentTemplate) -> OneOffSignatureResponse:
        assert template.case_id is not None
        assert template.case_step_progress_id is not None
        assert template.person_id is not None
        request = await self.repo.latest_request(template.id)
        return OneOffSignatureResponse(
            id=template.id,
            case_id=template.case_id,
            progress_id=template.case_step_progress_id,
            person_id=template.person_id,
            name=template.name,
            filename=template.filename,
            fields_configured=template.fields_configured,
            submitted_at=template.submitted_at,
            request_id=request.id if request else None,
            status=request.status if request else ("queued" if template.submitted_at else "draft"),
        )

    async def list(self, agent: Agent, case_id: uuid.UUID) -> list[OneOffSignatureResponse]:
        await self._case(agent, case_id)
        return [await self.response(row) for row in await self.repo.list(agent.agency_id, case_id)]

    async def create(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        *,
        progress_id: uuid.UUID,
        person_id: uuid.UUID,
        name: str,
        filename: str,
        content: bytes,
        content_type: str | None,
    ) -> OneOffSignatureResponse:
        await self._case(agent, case_id)
        progress, _person = await self._target(case_id, progress_id, person_id)
        name = name.strip()
        if not name or len(name) > 100:
            raise ValidationError(
                "A document name of up to 100 characters is required.",
                code="signatures.document_name_invalid",
            )
        if not content.startswith(b"%PDF-"):
            raise ValidationError("The file must be a PDF.", code="document_template.not_pdf")
        if await self.repo.reference_exists(progress, name):
            raise ConflictError(
                "This document name already exists on this step.",
                code="signatures.document_name_exists",
            )
        template = await DocumentTemplatesManager(self.db, case_id=case_id).create(
            agent,
            name=name,
            filename=filename,
            content=content,
            content_type=content_type,
            progress_id=progress_id,
            person_id=person_id,
        )
        return await self.response(template)

    async def builder_token(self, agent: Agent, case_id: uuid.UUID, draft_id: uuid.UUID) -> str:
        template = await self._draft(agent, case_id, draft_id)
        self._editable(template)
        return await DocumentTemplatesManager(self.db, case_id=case_id).builder_token(
            agent, draft_id
        )

    async def sync(
        self, agent: Agent, case_id: uuid.UUID, draft_id: uuid.UUID
    ) -> OneOffSignatureResponse:
        template = await self._draft(agent, case_id, draft_id)
        self._editable(template)
        template = await DocumentTemplatesManager(self.db, case_id=case_id).builder_sync(
            agent, draft_id
        )
        return await self.response(template)

    async def delete(self, agent: Agent, case_id: uuid.UUID, draft_id: uuid.UUID) -> None:
        template = await self._draft(agent, case_id, draft_id)
        self._editable(template)
        await DocumentTemplatesManager(self.db, case_id=case_id).delete(agent, draft_id)

    async def submit(
        self, agent: Agent, case_id: uuid.UUID, draft_id: uuid.UUID
    ) -> OneOffSignatureResponse:
        template = await self._draft(agent, case_id, draft_id)
        # A repeated confirmation never creates another requirement or spends another credit.
        if template.submitted_at is not None:
            return await self.response(template)
        assert template.case_step_progress_id is not None and template.person_id is not None
        progress, person = await self._target(
            case_id, template.case_step_progress_id, template.person_id
        )
        if not template.fields_configured or template.roles_count != 1:
            raise ValidationError(
                "Place a signature field for the selected signer.",
                code="signatures.one_off_fields_required",
            )
        if await self.repo.reference_exists(progress, template.name):
            raise ConflictError(
                "This document name already exists on this step.",
                code="signatures.document_name_exists",
            )
        case = await self._case(agent, case_id)
        requirement = CaseStepRequirement(
            case_step_progress_id=progress.id,
            person_id=person.id,
            step_requirement_id=None,
            kind="document",
            reference=template.name,
            scope="principal",
            signature_required=True,
            signature_level="ses",
            document_template_id=template.id,
            status="pending",
        )
        self.db.add(requirement)
        template.submitted_at = datetime.now(UTC)
        await self.db.flush()
        if progress.status == StepStatus.IN_PROGRESS.value:
            await SignaturesManager(self.db).send_one_off(case, progress, requirement)
        ActivityManager(self.db).log_action(
            case_id=case.id,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            action_type="step.requirement_added",
            details={
                "step_progress_id": str(progress.id),
                "kind": "document",
                "reference": template.name,
                "created": 1,
                "one_off": True,
            },
        )
        await self.db.commit()
        if progress.status == StepStatus.IN_PROGRESS.value:
            await ProgressManager(self.db).notify_requirement_added(case, progress)
        return await self.response(template)
