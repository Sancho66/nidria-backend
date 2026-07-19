import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from src.auth.auth_schema import MessageResponse
from src.comments.comments_manager import CommentsManager
from src.comments.comments_schema import (
    CommentCreateRequest,
    CommentResponse,
    CommentUpdateRequest,
)
from src.core.dependencies import get_current_agent, get_current_expat, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

# Per-step comment thread (VAGUE 5). Reading uses case.view (anyone who
# sees the case sees the thread); writing/editing uses the dedicated
# case.comment — posting to a CLIENT-VISIBLE channel is a capability
# distinct from viewing or editing the dossier. The expat face is
# ownership-gated (no matrix), the principal of their own cases only.
agent_router = APIRouter(prefix="/cases", tags=["comments"])
expat_router = APIRouter(prefix="/expat/cases", tags=["comments-expat"])

_VIEW = Permission.CASE_VIEW
_COMMENT = Permission.CASE_COMMENT
_A = "/cases/{case_id}/steps/{progress_id}/comments"
_AC = "/cases/{case_id}/steps/{progress_id}/comments/{comment_id}"
_E = "/expat/cases/{case_id}/steps/{progress_id}/comments"
_EC = "/expat/cases/{case_id}/steps/{progress_id}/comments/{comment_id}"

BINDINGS = [
    RouteBinding("GET", _A, Audience.AGENT, _VIEW),
    RouteBinding("POST", _A, Audience.AGENT, _COMMENT),
    RouteBinding("PATCH", _AC, Audience.AGENT, _COMMENT),
    RouteBinding("DELETE", _AC, Audience.AGENT, _COMMENT),
    RouteBinding("GET", _E, Audience.EXPAT),
    RouteBinding("POST", _E, Audience.EXPAT),
    RouteBinding("PATCH", _EC, Audience.EXPAT),
    RouteBinding("DELETE", _EC, Audience.EXPAT),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]
ExpatDep = Annotated[ExpatUser, Depends(get_current_expat)]

_SUB = "/{case_id}/steps/{progress_id}/comments"
_SUB_ID = "/{case_id}/steps/{progress_id}/comments/{comment_id}"


# --- agent face ----------------------------------------------------------------------


@agent_router.get(_SUB, response_model=list[CommentResponse])
async def list_comments_as_agent(
    case_id: uuid.UUID, progress_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[CommentResponse]:
    return await CommentsManager(db).list_as_agent(agent, case_id, progress_id)


@agent_router.post(_SUB, response_model=CommentResponse, status_code=201)
async def create_comment_as_agent(
    case_id: uuid.UUID,
    progress_id: uuid.UUID,
    body: CommentCreateRequest,
    agent: AgentDep,
    db: DbDep,
) -> CommentResponse:
    return await CommentsManager(db).create_as_agent(
        agent, case_id, progress_id, body.body, body.document_id
    )


@agent_router.patch(_SUB_ID, response_model=CommentResponse)
async def update_comment_as_agent(
    case_id: uuid.UUID,
    progress_id: uuid.UUID,
    comment_id: uuid.UUID,
    body: CommentUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> CommentResponse:
    return await CommentsManager(db).update_as_agent(
        agent, case_id, progress_id, comment_id, body.body
    )


@agent_router.delete(_SUB_ID, response_model=MessageResponse)
async def delete_comment_as_agent(
    case_id: uuid.UUID, progress_id: uuid.UUID, comment_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await CommentsManager(db).delete_as_agent(agent, case_id, progress_id, comment_id)
    return MessageResponse(detail="Comment deleted.")


# --- expat face ----------------------------------------------------------------------


@expat_router.get(_SUB, response_model=list[CommentResponse])
async def list_comments_as_expat(
    case_id: uuid.UUID, progress_id: uuid.UUID, expat: ExpatDep, db: DbDep
) -> list[CommentResponse]:
    return await CommentsManager(db).list_as_expat(expat, case_id, progress_id)


@expat_router.post(_SUB, response_model=CommentResponse, status_code=201)
async def create_comment_as_expat(
    case_id: uuid.UUID,
    progress_id: uuid.UUID,
    body: CommentCreateRequest,
    expat: ExpatDep,
    db: DbDep,
) -> CommentResponse:
    return await CommentsManager(db).create_as_expat(
        expat, case_id, progress_id, body.body, body.document_id
    )


@expat_router.patch(_SUB_ID, response_model=CommentResponse)
async def update_comment_as_expat(
    case_id: uuid.UUID,
    progress_id: uuid.UUID,
    comment_id: uuid.UUID,
    body: CommentUpdateRequest,
    expat: ExpatDep,
    db: DbDep,
) -> CommentResponse:
    return await CommentsManager(db).update_as_expat(
        expat, case_id, progress_id, comment_id, body.body
    )


@expat_router.delete(_SUB_ID, response_model=MessageResponse)
async def delete_comment_as_expat(
    case_id: uuid.UUID, progress_id: uuid.UUID, comment_id: uuid.UUID, expat: ExpatDep, db: DbDep
) -> MessageResponse:
    await CommentsManager(db).delete_as_expat(expat, case_id, progress_id, comment_id)
    return MessageResponse(detail="Comment deleted.")
