import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.document import Document


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

    async def list_documents(self, case_id: uuid.UUID) -> list[Document]:
        stmt = (
            select(Document).where(Document.case_id == case_id).order_by(Document.created_at.desc())
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_document_in_case(
        self, case_id: uuid.UUID, document_id: uuid.UUID
    ) -> Document | None:
        stmt = select(Document).where(Document.id == document_id, Document.case_id == case_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_progress_in_case(
        self, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> CaseStepProgress | None:
        stmt = select(CaseStepProgress).where(
            CaseStepProgress.id == progress_id, CaseStepProgress.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_document(self, **kwargs: Any) -> Document:
        document = Document(**kwargs)
        self.db.add(document)
        return document

    async def delete_row(self, row: Document) -> None:
        await self.db.delete(row)
