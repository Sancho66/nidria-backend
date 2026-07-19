import mimetypes
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.auth.auth_schema import MessageResponse
from src.comments.comments_manager import CommentsManager
from src.comments.comments_schema import (
    CommentCreateRequest,
    CommentResponse,
    CommentUpdateRequest,
)
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.http import file_download_response
from src.core.i18n import RequestLang
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.documents.documents_manager import DocumentsManager
from src.external.external_manager import ExternalAssignmentManager, ExternalPortalManager
from src.external.external_schema import (
    ExternalAssignmentCreateRequest,
    ExternalAssignmentResponse,
    ExternalCaseDetailResponse,
    ExternalCaseSummaryResponse,
    ExternalDocumentResponse,
)

# Provider portal: AGENT audience (an external IS an agent), gated by the
# external.* permissions, EVERY route scoped by assignment in the manager.
# These routes are the ONLY ones the wave-A guard lets an external reach
# (allowlist), and the guard still denies all internal /cases/* routes.
external_router = APIRouter(prefix="/external/cases", tags=["external-portal"])
# Agency side: who may access a client's data = an admin act (agent.manage),
# not a plain case edit. Internal actors only (the guard never applies to
# them; externals lack agent.manage anyway).
agency_router = APIRouter(prefix="/cases", tags=["external-assignments"])

_VIEW = Permission.EXTERNAL_CASE_VIEW
_UPLOAD = Permission.EXTERNAL_DOCUMENT_UPLOAD
_COMMENT = Permission.EXTERNAL_CASE_COMMENT
_VALIDATE = Permission.EXTERNAL_STEP_VALIDATE
_MANAGE = Permission.AGENT_MANAGE

_C = "/external/cases/{case_id}"
_ATT = "/external/cases/{case_id}/steps/{progress_id}/attachments/{attachment_id}/download"
_VALIDATE_ROUTE = "/external/cases/{case_id}/steps/{progress_id}/validate"
_CMT = "/external/cases/{case_id}/steps/{progress_id}/comments"
_CMT_ID = "/external/cases/{case_id}/steps/{progress_id}/comments/{comment_id}"
_ASG = "/cases/{case_id}/external-assignments"

BINDINGS = [
    # Provider portal (each scoped by get_case_for_external).
    RouteBinding("GET", "/external/cases", Audience.AGENT, _VIEW),
    RouteBinding("GET", _C, Audience.AGENT, _VIEW),
    RouteBinding("GET", f"{_C}/documents", Audience.AGENT, _VIEW),
    RouteBinding("GET", f"{_C}/documents/{{document_id}}/download", Audience.AGENT, _VIEW),
    RouteBinding("POST", f"{_C}/requirements/{{requirement_id}}/document", Audience.AGENT, _UPLOAD),
    # GAP-B : le prestataire LIVRE sur une etape du dossier assigne (la
    # traduction certifiee) — meme perimetre que tous ses acces.
    RouteBinding("POST", f"{_C}/documents", Audience.AGENT, _UPLOAD),
    # Feature 2 (RGPD): step attachment download — gated in the manager to
    # steps this provider is responsible for (responsible_agent_id).
    RouteBinding("GET", _ATT, Audience.AGENT, _VIEW),
    # "Action validée par" = provider: gated in the manager to the step's
    # designated validator (validated_by_agent_id == external.id).
    RouteBinding("POST", _VALIDATE_ROUTE, Audience.AGENT, _VALIDATE),
    RouteBinding("GET", _CMT, Audience.AGENT, _VIEW),
    RouteBinding("POST", _CMT, Audience.AGENT, _COMMENT),
    RouteBinding("PATCH", _CMT_ID, Audience.AGENT, _COMMENT),
    RouteBinding("DELETE", _CMT_ID, Audience.AGENT, _COMMENT),
    # Agency-side assignment management.
    RouteBinding("GET", _ASG, Audience.AGENT, _MANAGE),
    RouteBinding("POST", _ASG, Audience.AGENT, _MANAGE),
    RouteBinding("DELETE", f"{_ASG}/{{agent_id}}", Audience.AGENT, _MANAGE),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


# --- provider portal -----------------------------------------------------------------


@external_router.get("", response_model=list[ExternalCaseSummaryResponse])
async def list_my_cases(agent: AgentDep, db: DbDep) -> list[ExternalCaseSummaryResponse]:
    return await ExternalPortalManager(db).list_my_cases(agent)


@external_router.get("/{case_id}", response_model=ExternalCaseDetailResponse)
async def get_my_case(
    case_id: uuid.UUID, agent: AgentDep, db: DbDep, lang: RequestLang
) -> ExternalCaseDetailResponse:
    return await ExternalPortalManager(db).get_my_case(agent, case_id, lang)


@external_router.get("/{case_id}/documents", response_model=list[ExternalDocumentResponse])
async def list_documents(
    case_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[ExternalDocumentResponse]:
    return await DocumentsManager(db).list_for_external(agent, case_id)


@external_router.get("/{case_id}/documents/{document_id}/download")
async def download_document(
    case_id: uuid.UUID, document_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> Response:
    document, content = await DocumentsManager(db).download_for_external(
        agent, case_id, document_id
    )
    media_type = mimetypes.guess_type(document.filename)[0] or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{document.filename}"'},
    )


@external_router.post(
    "/{case_id}/steps/{progress_id}/validate", response_model=ExternalCaseDetailResponse
)
async def validate_step(
    case_id: uuid.UUID, progress_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> ExternalCaseDetailResponse:
    return await ExternalPortalManager(db).validate_step(agent, case_id, progress_id)


@external_router.get("/{case_id}/steps/{progress_id}/attachments/{attachment_id}/download")
async def download_step_attachment(
    case_id: uuid.UUID,
    progress_id: uuid.UUID,
    attachment_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
) -> Response:
    filename, content = await ExternalPortalManager(db).download_step_attachment(
        agent, case_id, progress_id, attachment_id
    )
    return file_download_response(filename, content)


@external_router.post(
    "/{case_id}/documents", response_model=ExternalDocumentResponse, status_code=201
)
async def upload_step_document(
    case_id: uuid.UUID,
    file: UploadFile,
    agent: AgentDep,
    db: DbDep,
    step_progress_id: Annotated[uuid.UUID | None, Form()] = None,
    kind: Annotated[Literal["deposit", "deliverable"], Form()] = "deliverable",
    person_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ExternalDocumentResponse:
    """Le livrable du prestataire (GAP-B) : depose sur l'etape DU dossier
    assigne, visible par le client — deliverable par defaut."""
    return await DocumentsManager(db).upload_step_document_as_external(
        agent, case_id, file, step_progress_id, kind=kind, person_id=person_id
    )


@external_router.post(
    "/{case_id}/requirements/{requirement_id}/document",
    response_model=ExternalDocumentResponse,
    status_code=201,
)
async def fulfill_requirement_document(
    case_id: uuid.UUID,
    requirement_id: uuid.UUID,
    file: UploadFile,
    agent: AgentDep,
    db: DbDep,
) -> ExternalDocumentResponse:
    return await DocumentsManager(db).fulfill_requirement_as_external(
        agent, case_id, requirement_id, file
    )


@external_router.get(
    "/{case_id}/steps/{progress_id}/comments", response_model=list[CommentResponse]
)
async def list_comments(
    case_id: uuid.UUID, progress_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[CommentResponse]:
    return await CommentsManager(db).list_as_external(agent, case_id, progress_id)


@external_router.post(
    "/{case_id}/steps/{progress_id}/comments", response_model=CommentResponse, status_code=201
)
async def create_comment(
    case_id: uuid.UUID,
    progress_id: uuid.UUID,
    body: CommentCreateRequest,
    agent: AgentDep,
    db: DbDep,
) -> CommentResponse:
    return await CommentsManager(db).create_as_external(agent, case_id, progress_id, body.body)


@external_router.patch(
    "/{case_id}/steps/{progress_id}/comments/{comment_id}", response_model=CommentResponse
)
async def update_comment(
    case_id: uuid.UUID,
    progress_id: uuid.UUID,
    comment_id: uuid.UUID,
    body: CommentUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> CommentResponse:
    return await CommentsManager(db).update_as_external(
        agent, case_id, progress_id, comment_id, body.body
    )


@external_router.delete(
    "/{case_id}/steps/{progress_id}/comments/{comment_id}", response_model=MessageResponse
)
async def delete_comment(
    case_id: uuid.UUID, progress_id: uuid.UUID, comment_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await CommentsManager(db).delete_as_external(agent, case_id, progress_id, comment_id)
    return MessageResponse(detail="Comment deleted.")


# --- agency-side assignment management (agent.manage) --------------------------------


@agency_router.get(
    "/{case_id}/external-assignments", response_model=list[ExternalAssignmentResponse]
)
async def list_assignments(
    case_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[ExternalAssignmentResponse]:
    return await ExternalAssignmentManager(db).list_assignments(agent, case_id)


@agency_router.post(
    "/{case_id}/external-assignments", response_model=ExternalAssignmentResponse, status_code=201
)
async def create_assignment(
    case_id: uuid.UUID, body: ExternalAssignmentCreateRequest, agent: AgentDep, db: DbDep
) -> ExternalAssignmentResponse:
    return await ExternalAssignmentManager(db).assign(agent, case_id, body.agent_id)


@agency_router.delete("/{case_id}/external-assignments/{agent_id}", response_model=MessageResponse)
async def delete_assignment(
    case_id: uuid.UUID, agent_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await ExternalAssignmentManager(db).unassign(agent, case_id, agent_id)
    return MessageResponse(detail="Assignment removed.")
