import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from src.activity.activity_manager import ActivityManager
from src.core import storage
from src.core.config import get_settings
from src.core.enums import ActorType, DocValidationStatus, StepRequirementKind, StepStatus
from src.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from src.documents.documents_repository import DocumentsRepository
from src.documents.documents_schema import (
    DocumentResponse,
    DocumentValidationRequest,
    ExpatDocumentResponse,
)
from src.progress.progress_manager import ProgressManager


class DocumentsManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = DocumentsRepository(db)
        self.activity = ActivityManager(db)

    # --- case resolution, per audience ---------------------------------------------

    async def _case_for_agent(self, agent: Agent, case_id: uuid.UUID) -> ClientCase:
        case = await self.repo.get_case_in_agency(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        return case

    async def _case_for_expat(self, expat: ExpatUser, case_id: uuid.UUID) -> ClientCase:
        # Strict ownership: 404, never 403 — a foreign case's existence
        # must not be revealed.
        case = await self.repo.get_case_for_expat(expat.id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        return case

    def _log(
        self,
        case_id: uuid.UUID,
        actor_type: ActorType,
        actor_id: uuid.UUID,
        action_type: str,
        details: dict[str, Any],
    ) -> None:
        self.activity.log_action(
            case_id=case_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action_type=action_type,
            details=details,
        )

    # --- upload ------------------------------------------------------------------------

    async def _upload(
        self,
        case: ClientCase,
        file: UploadFile,
        step_progress_id: uuid.UUID | None,
        expires_at: datetime | None,
        actor_type: ActorType,
        actor_id: uuid.UUID,
    ) -> Document:
        settings = get_settings()
        original_filename = file.filename
        if not original_filename:
            raise ValidationError("A filename is required.")
        extension = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else ""
        if extension not in settings.allowed_document_extensions:
            allowed = ", ".join(settings.allowed_document_extensions)
            raise ValidationError(f"File type not allowed (accepted: {allowed}).")

        content = await file.read()
        if len(content) > settings.max_document_size_mb * 1024 * 1024:
            raise PayloadTooLargeError(
                f"File exceeds the {settings.max_document_size_mb} MB limit."
            )

        if step_progress_id is not None and (
            await self.repo.get_progress_in_case(case.id, step_progress_id) is None
        ):
            raise ValidationError("step_progress_id does not belong to this case.")

        # Strictly sanitized KEY; the ORIGINAL filename stays in DB for
        # display (the path is technical, the name is data).
        document_id = uuid.uuid4()
        path = f"{case.id}/{document_id}/{storage.sanitize_filename(original_filename)}"
        await asyncio.to_thread(
            storage.upload, path, content, file.content_type or "application/octet-stream"
        )

        document = self.repo.add_document(
            id=document_id,
            case_id=case.id,
            step_progress_id=step_progress_id,
            filename=original_filename,
            storage_path=path,
            uploaded_by_type=actor_type.value,
            uploaded_by_id=actor_id,
            expires_at=expires_at,
        )
        self._log(
            case.id,
            actor_type,
            actor_id,
            "document.uploaded",
            {
                "document_id": str(document_id),
                "filename": original_filename,
                "step_progress_id": str(step_progress_id) if step_progress_id else None,
            },
        )
        await self.db.commit()
        await self.db.refresh(document)
        return document

    async def upload_as_agent(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        file: UploadFile,
        step_progress_id: uuid.UUID | None,
        expires_at: datetime | None,
    ) -> Document:
        case = await self._case_for_agent(agent, case_id)
        return await self._upload(
            case, file, step_progress_id, expires_at, ActorType.AGENT, agent.id
        )

    async def upload_as_expat(
        self,
        expat: ExpatUser,
        case_id: uuid.UUID,
        file: UploadFile,
        step_progress_id: uuid.UUID | None,
    ) -> Document:
        case = await self._case_for_expat(expat, case_id)
        return await self._upload(case, file, step_progress_id, None, ActorType.EXPAT, expat.id)

    async def fulfill_requirement_as_agent(
        self, agent: Agent, case_id: uuid.UUID, requirement_id: uuid.UUID, file: UploadFile
    ) -> Document:
        """Agent equivalent of the wave-2 expat requirement upload (the
        gap): upload + link the doc to the requirement + mark provided +
        recompute (auto→DONE). Same shared core as the client path
        (ProgressManager.fulfill_document_requirement); only the perimeter
        (agency scope) and the upload audience differ."""
        case = await self._case_for_agent(agent, case_id)  # border 1
        found = await self.repo.get_requirement_in_case(case.id, requirement_id)  # border 2
        if found is None:
            raise NotFoundError("Requirement not found.")
        requirement, progress = found
        if progress.status != StepStatus.IN_PROGRESS.value:  # border 3 (mirror expat)
            raise ConflictError("This step is not active; its requirements are read-only.")
        if requirement.kind != StepRequirementKind.DOCUMENT.value:
            raise ValidationError("This requirement does not expect a document.")

        document = await self._upload(
            case, file, requirement.case_step_progress_id, None, ActorType.AGENT, agent.id
        )
        progress_mgr = ProgressManager(self.db)
        pending = await progress_mgr.fulfill_document_requirement(case, requirement, document.id)
        await self.db.commit()
        await progress_mgr.send_pending(pending)
        return document

    # --- read ----------------------------------------------------------------------------

    async def _enrich(
        self, documents: list[Document]
    ) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
        """Batched aggregated-view context (no N+1): step names by
        progress id, requirement references by document id (inverse join)."""
        step_names = await self.repo.step_names(
            [d.step_progress_id for d in documents if d.step_progress_id is not None]
        )
        req_refs = await self.repo.requirement_refs([d.id for d in documents])
        return step_names, req_refs

    async def list_for_agent(self, agent: Agent, case_id: uuid.UUID) -> list[DocumentResponse]:
        case = await self._case_for_agent(agent, case_id)
        documents = await self.repo.list_documents(case.id)
        step_names, req_refs = await self._enrich(documents)
        return [
            DocumentResponse(
                id=d.id,
                case_id=d.case_id,
                step_progress_id=d.step_progress_id,
                filename=d.filename,
                uploaded_by_type=d.uploaded_by_type,
                uploaded_by_id=d.uploaded_by_id,
                validation_status=d.validation_status,
                expires_at=d.expires_at,
                created_at=d.created_at,
                step_name=step_names.get(d.step_progress_id) if d.step_progress_id else None,
                requirement_reference=req_refs.get(d.id),
                is_requirement=d.id in req_refs,
            )
            for d in documents
        ]

    async def list_for_expat(
        self, expat: ExpatUser, case_id: uuid.UUID
    ) -> list[ExpatDocumentResponse]:
        # The expat sees ALL documents of their case — the agency
        # deposits pieces FOR the client (validated decision).
        case = await self._case_for_expat(expat, case_id)
        documents = await self.repo.list_documents(case.id)
        step_names, req_refs = await self._enrich(documents)
        return [
            ExpatDocumentResponse(
                id=d.id,
                case_id=d.case_id,
                filename=d.filename,
                uploaded_by_type=d.uploaded_by_type,
                # is_mine drives the delete affordance; no internal UUID exposed.
                is_mine=(
                    d.uploaded_by_type == ActorType.EXPAT.value and d.uploaded_by_id == expat.id
                ),
                validation_status=d.validation_status,
                expires_at=d.expires_at,
                created_at=d.created_at,
                step_name=step_names.get(d.step_progress_id) if d.step_progress_id else None,
                requirement_reference=req_refs.get(d.id),
                is_requirement=d.id in req_refs,
            )
            for d in documents
        ]

    async def _download(self, case: ClientCase, document_id: uuid.UUID) -> tuple[Document, bytes]:
        document = await self.repo.get_document_in_case(case.id, document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        content = await asyncio.to_thread(storage.download, document.storage_path)
        return document, content

    async def download_for_agent(
        self, agent: Agent, case_id: uuid.UUID, document_id: uuid.UUID
    ) -> tuple[Document, bytes]:
        case = await self._case_for_agent(agent, case_id)
        return await self._download(case, document_id)

    async def download_for_expat(
        self, expat: ExpatUser, case_id: uuid.UUID, document_id: uuid.UUID
    ) -> tuple[Document, bytes]:
        case = await self._case_for_expat(expat, case_id)
        return await self._download(case, document_id)

    # --- validation -----------------------------------------------------------------------

    async def validate_document(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        document_id: uuid.UUID,
        payload: DocumentValidationRequest,
    ) -> Document:
        case = await self._case_for_agent(agent, case_id)
        document = await self.repo.get_document_in_case(case.id, document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        old_status = document.validation_status
        document.validation_status = payload.validation_status.value
        if "expires_at" in payload.model_fields_set:
            document.expires_at = payload.expires_at
        self._log(
            case.id,
            ActorType.AGENT,
            agent.id,
            "document.validated",
            {
                "document_id": str(document.id),
                "old": old_status,
                "new": document.validation_status,
            },
        )
        await self.db.commit()
        await self.db.refresh(document)
        return document

    # --- delete ----------------------------------------------------------------------------

    async def _delete(
        self,
        case: ClientCase,
        document: Document,
        actor_type: ActorType,
        actor_id: uuid.UUID,
    ) -> None:
        details = {"document_id": str(document.id), "filename": document.filename}
        await asyncio.to_thread(storage.delete, document.storage_path)
        await self.repo.delete_row(document)
        self._log(case.id, actor_type, actor_id, "document.deleted", details)
        await self.db.commit()

    async def delete_as_agent(
        self, agent: Agent, case_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        case = await self._case_for_agent(agent, case_id)
        document = await self.repo.get_document_in_case(case.id, document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        await self._delete(case, document, ActorType.AGENT, agent.id)

    async def delete_as_expat(
        self, expat: ExpatUser, case_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        case = await self._case_for_expat(expat, case_id)
        document = await self.repo.get_document_in_case(case.id, document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        if (
            document.uploaded_by_type != ActorType.EXPAT.value
            or document.uploaded_by_id != expat.id
        ):
            raise ForbiddenError("Only your own uploads can be deleted.")
        if document.validation_status == DocValidationStatus.OK.value:
            # An OK-validated piece is frozen in the file; NULL /
            # INCOMPLETE / TO_FIX stay deletable (replace flow).
            raise ForbiddenError("A validated document cannot be deleted.")
        await self._delete(case, document, ActorType.EXPAT, expat.id)
