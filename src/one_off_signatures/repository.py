import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.document_template import DocumentTemplate
from shared.models.signature import SignatureRequest, SignatureSigner
from shared.models.step_requirement import StepRequirement


class OneOffSignaturesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def case(self, agency_id: uuid.UUID, case_id: uuid.UUID) -> ClientCase | None:
        return (
            await self.db.execute(
                select(ClientCase).where(
                    ClientCase.id == case_id,
                    ClientCase.agency_id == agency_id,
                    ClientCase.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def progress(self, case_id: uuid.UUID, progress_id: uuid.UUID) -> CaseStepProgress | None:
        # Serialize confirmation with step activation/completion and other new documents.
        return (
            await self.db.execute(
                select(CaseStepProgress)
                .where(
                    CaseStepProgress.id == progress_id,
                    CaseStepProgress.case_id == case_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def person(self, case_id: uuid.UUID, person_id: uuid.UUID) -> CasePerson | None:
        return (
            await self.db.execute(
                select(CasePerson).where(
                    CasePerson.id == person_id,
                    CasePerson.case_id == case_id,
                )
            )
        ).scalar_one_or_none()

    async def list(self, agency_id: uuid.UUID, case_id: uuid.UUID) -> list[DocumentTemplate]:
        return list(
            await self.db.scalars(
                select(DocumentTemplate)
                .where(
                    DocumentTemplate.agency_id == agency_id,
                    DocumentTemplate.case_id == case_id,
                )
                .order_by(DocumentTemplate.created_at.desc())
                .limit(100)
            )
        )

    async def draft(
        self, agency_id: uuid.UUID, case_id: uuid.UUID, draft_id: uuid.UUID
    ) -> DocumentTemplate | None:
        return (
            await self.db.execute(
                select(DocumentTemplate)
                .where(
                    DocumentTemplate.id == draft_id,
                    DocumentTemplate.agency_id == agency_id,
                    DocumentTemplate.case_id == case_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def reference_exists(self, row: CaseStepProgress, name: str) -> bool:
        concrete = exists().where(
            CaseStepRequirement.case_step_progress_id == row.id,
            CaseStepRequirement.kind == "document",
            CaseStepRequirement.reference == name,
        )
        definition = exists().where(
            StepRequirement.step_id == row.template_step_id,
            StepRequirement.kind == "document",
            StepRequirement.reference == name,
        )
        return bool(await self.db.scalar(select(concrete | definition)))

    async def latest_request(self, template_id: uuid.UUID) -> SignatureRequest | None:
        return (
            await self.db.execute(
                select(SignatureRequest)
                .join(SignatureSigner, SignatureSigner.signature_request_id == SignatureRequest.id)
                .join(
                    CaseStepRequirement,
                    CaseStepRequirement.id == SignatureSigner.case_step_requirement_id,
                )
                .where(CaseStepRequirement.document_template_id == template_id)
                .order_by(SignatureRequest.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
