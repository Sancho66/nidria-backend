import mimetypes
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from src.auth.auth_schema import MessageResponse
from src.core.dependencies import get_current_agent, get_current_expat, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.documents.documents_manager import DocumentsManager
from src.documents.documents_schema import DocumentResponse, DocumentValidationRequest

agent_router = APIRouter(prefix="/cases", tags=["documents"])
expat_router = APIRouter(prefix="/expat/cases", tags=["documents-expat"])

BINDINGS = [
    # Agent side: uploading/deleting = working the case (case.edit);
    # validating = committing the agency (document.validate).
    RouteBinding("POST", "/cases/{case_id}/documents", Audience.AGENT, Permission.CASE_EDIT),
    RouteBinding("GET", "/cases/{case_id}/documents", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding(
        "GET",
        "/cases/{case_id}/documents/{document_id}/download",
        Audience.AGENT,
        Permission.CASE_VIEW,
    ),
    RouteBinding(
        "PATCH",
        "/cases/{case_id}/documents/{document_id}/validation",
        Audience.AGENT,
        Permission.DOCUMENT_VALIDATE,
    ),
    RouteBinding(
        "DELETE",
        "/cases/{case_id}/documents/{document_id}",
        Audience.AGENT,
        Permission.CASE_EDIT,
    ),
    # Expat side: audience token only, strict ownership in the Manager.
    RouteBinding("POST", "/expat/cases/{case_id}/documents", Audience.EXPAT),
    RouteBinding("GET", "/expat/cases/{case_id}/documents", Audience.EXPAT),
    RouteBinding("GET", "/expat/cases/{case_id}/documents/{document_id}/download", Audience.EXPAT),
    RouteBinding("DELETE", "/expat/cases/{case_id}/documents/{document_id}", Audience.EXPAT),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]
ExpatDep = Annotated[ExpatUser, Depends(get_current_expat)]


def _download_response(document: Document, content: bytes) -> Response:
    media_type = mimetypes.guess_type(document.filename)[0] or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{document.filename}"'},
    )


# --- agent side -------------------------------------------------------------------


@agent_router.post("/{case_id}/documents", response_model=DocumentResponse, status_code=201)
async def upload_document_as_agent(
    case_id: uuid.UUID,
    file: UploadFile,
    agent: AgentDep,
    db: DbDep,
    step_progress_id: Annotated[uuid.UUID | None, Form()] = None,
    expires_at: Annotated[datetime | None, Form()] = None,
) -> DocumentResponse:
    document = await DocumentsManager(db).upload_as_agent(
        agent, case_id, file, step_progress_id, expires_at
    )
    return DocumentResponse.model_validate(document)


@agent_router.get("/{case_id}/documents", response_model=list[DocumentResponse])
async def list_documents_as_agent(
    case_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[DocumentResponse]:
    documents = await DocumentsManager(db).list_for_agent(agent, case_id)
    return [DocumentResponse.model_validate(document) for document in documents]


@agent_router.get("/{case_id}/documents/{document_id}/download")
async def download_document_as_agent(
    case_id: uuid.UUID, document_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> Response:
    document, content = await DocumentsManager(db).download_for_agent(agent, case_id, document_id)
    return _download_response(document, content)


@agent_router.patch(
    "/{case_id}/documents/{document_id}/validation", response_model=DocumentResponse
)
async def validate_document(
    case_id: uuid.UUID,
    document_id: uuid.UUID,
    body: DocumentValidationRequest,
    agent: AgentDep,
    db: DbDep,
) -> DocumentResponse:
    document = await DocumentsManager(db).validate_document(agent, case_id, document_id, body)
    return DocumentResponse.model_validate(document)


@agent_router.delete("/{case_id}/documents/{document_id}", response_model=MessageResponse)
async def delete_document_as_agent(
    case_id: uuid.UUID, document_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await DocumentsManager(db).delete_as_agent(agent, case_id, document_id)
    return MessageResponse(detail="Document deleted.")


# --- expat side --------------------------------------------------------------------


@expat_router.post("/{case_id}/documents", response_model=DocumentResponse, status_code=201)
async def upload_document_as_expat(
    case_id: uuid.UUID,
    file: UploadFile,
    expat: ExpatDep,
    db: DbDep,
    step_progress_id: Annotated[uuid.UUID | None, Form()] = None,
) -> DocumentResponse:
    document = await DocumentsManager(db).upload_as_expat(expat, case_id, file, step_progress_id)
    return DocumentResponse.model_validate(document)


@expat_router.get("/{case_id}/documents", response_model=list[DocumentResponse])
async def list_documents_as_expat(
    case_id: uuid.UUID, expat: ExpatDep, db: DbDep
) -> list[DocumentResponse]:
    documents = await DocumentsManager(db).list_for_expat(expat, case_id)
    return [DocumentResponse.model_validate(document) for document in documents]


@expat_router.get("/{case_id}/documents/{document_id}/download")
async def download_document_as_expat(
    case_id: uuid.UUID, document_id: uuid.UUID, expat: ExpatDep, db: DbDep
) -> Response:
    document, content = await DocumentsManager(db).download_for_expat(expat, case_id, document_id)
    return _download_response(document, content)


@expat_router.delete("/{case_id}/documents/{document_id}", response_model=MessageResponse)
async def delete_document_as_expat(
    case_id: uuid.UUID, document_id: uuid.UUID, expat: ExpatDep, db: DbDep
) -> MessageResponse:
    await DocumentsManager(db).delete_as_expat(expat, case_id, document_id)
    return MessageResponse(detail="Document deleted.")
