import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.journey import JourneyTemplateStep


class DocumentsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_case_in_agency(
        self, agency_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        stmt = select(ClientCase).where(ClientCase.id == case_id, ClientCase.agency_id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_case_for_expat(
        self, expat_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        stmt = select(ClientCase).where(
            ClientCase.id == case_id,
            ClientCase.principal_expat_user_id == expat_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_documents(
        self, case_id: uuid.UUID, step_progress_id: uuid.UUID | None = None
    ) -> list[Document]:
        stmt = (
            select(Document).where(Document.case_id == case_id).order_by(Document.created_at.desc())
        )
        if step_progress_id is not None:
            stmt = stmt.where(Document.step_progress_id == step_progress_id)
        return list((await self.db.execute(stmt)).scalars())

    async def get_person_in_case(
        self, case_id: uuid.UUID, person_id: uuid.UUID
    ) -> CasePerson | None:
        stmt = select(CasePerson).where(CasePerson.case_id == case_id, CasePerson.id == person_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_document_in_case(
        self, case_id: uuid.UUID, document_id: uuid.UUID
    ) -> Document | None:
        stmt = select(Document).where(Document.id == document_id, Document.case_id == case_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    # --- member (person-scoped) document access, decision B (no migration) ---------
    #
    # A dossier MEMBER sees ONLY documents reachable through THEIR OWN
    # requirements (case_step_requirement.document_id, person-keyed). A document
    # not attached to one of their requirements is not theirs — including
    # agency step attachments, which carry no person and never surface.

    async def list_documents_for_person(
        self, case_id: uuid.UUID, person_id: uuid.UUID, step_progress_id: uuid.UUID | None = None
    ) -> list[Document]:
        # Reachable through THEIR requirements (decision B) OR targeting
        # them nominatively (GAP-B: the deliverable for Claire).
        own_requirement_docs = (
            select(CaseStepRequirement.document_id)
            .join(
                CaseStepProgress,
                CaseStepProgress.id == CaseStepRequirement.case_step_progress_id,
            )
            .where(
                CaseStepProgress.case_id == case_id,
                CaseStepRequirement.person_id == person_id,
                CaseStepRequirement.document_id.is_not(None),
            )
        )
        stmt = (
            select(Document)
            .where(
                Document.case_id == case_id,
                or_(Document.id.in_(own_requirement_docs), Document.person_id == person_id),
            )
            .order_by(Document.created_at.desc())
            .distinct()
        )
        if step_progress_id is not None:
            stmt = stmt.where(Document.step_progress_id == step_progress_id)
        return list((await self.db.execute(stmt)).scalars())

    async def get_document_for_person(
        self, case_id: uuid.UUID, document_id: uuid.UUID, person_id: uuid.UUID
    ) -> Document | None:
        own_requirement_docs = (
            select(CaseStepRequirement.document_id)
            .join(
                CaseStepProgress,
                CaseStepProgress.id == CaseStepRequirement.case_step_progress_id,
            )
            .where(
                CaseStepProgress.case_id == case_id,
                CaseStepRequirement.person_id == person_id,
                CaseStepRequirement.document_id.is_not(None),
            )
        )
        stmt = (
            select(Document)
            .where(
                Document.id == document_id,
                Document.case_id == case_id,
                or_(Document.id.in_(own_requirement_docs), Document.person_id == person_id),
            )
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalars().first()

    async def get_progress_in_case(
        self, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> CaseStepProgress | None:
        stmt = select(CaseStepProgress).where(
            CaseStepProgress.id == progress_id, CaseStepProgress.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_requirement_in_case(
        self, case_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> tuple[CaseStepRequirement, CaseStepProgress] | None:
        """Requirement + its owning progress, scoped to the case (the
        agent perimeter — a requirement of another case is invisible)."""
        stmt = (
            select(CaseStepRequirement, CaseStepProgress)
            .join(
                CaseStepProgress,
                CaseStepProgress.id == CaseStepRequirement.case_step_progress_id,
            )
            .where(
                CaseStepRequirement.id == requirement_id,
                CaseStepProgress.case_id == case_id,
            )
        )
        row = (await self.db.execute(stmt)).first()
        return (row[0], row[1]) if row is not None else None

    async def step_names(self, progress_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """{case_step_progress_id: step name} — one join, batched."""
        if not progress_ids:
            return {}
        stmt = (
            select(CaseStepProgress.id, JourneyTemplateStep.name)
            .join(JourneyTemplateStep, JourneyTemplateStep.id == CaseStepProgress.template_step_id)
            .where(CaseStepProgress.id.in_(progress_ids))
        )
        return {pid: name for pid, name in (await self.db.execute(stmt)).all()}

    async def requirement_refs(self, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Inverse join {document_id: requirement reference} — a document
        is "linked" iff a case_step_requirement points at it. This is the
        ONLY classifier for linked-vs-free: a freely-uploaded doc may carry
        a step_progress_id yet answer no requirement."""
        if not document_ids:
            return {}
        stmt = select(CaseStepRequirement.document_id, CaseStepRequirement.reference).where(
            CaseStepRequirement.document_id.in_(document_ids)
        )
        return {doc_id: ref for doc_id, ref in (await self.db.execute(stmt)).all()}

    def add_document(self, **kwargs: Any) -> Document:
        document = Document(**kwargs)
        self.db.add(document)
        return document

    async def delete_row(self, row: Document) -> None:
        await self.db.delete(row)
