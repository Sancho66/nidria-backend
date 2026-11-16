import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.config import get_settings
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.document_templates.document_templates_schema import BuilderTokenResponse
from src.one_off_signatures.manager import OneOffSignaturesManager
from src.one_off_signatures.schema import OneOffSignatureResponse

PREFIX = "/cases/{case_id}/signature-drafts"
router = APIRouter(prefix=PREFIX, tags=["one-off-signatures"])
DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]

BINDINGS = [
    RouteBinding("GET", PREFIX, Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("POST", PREFIX, Audience.AGENT, Permission.CASE_EDIT),
    RouteBinding("DELETE", PREFIX + "/{draft_id}", Audience.AGENT, Permission.CASE_EDIT),
    RouteBinding(
        "POST", PREFIX + "/{draft_id}/builder-token", Audience.AGENT, Permission.CASE_EDIT
    ),
    RouteBinding("POST", PREFIX + "/{draft_id}/builder-sync", Audience.AGENT, Permission.CASE_EDIT),
    RouteBinding("POST", PREFIX + "/{draft_id}/submit", Audience.AGENT, Permission.CASE_EDIT),
]


@router.get("", response_model=list[OneOffSignatureResponse])
async def list_drafts(
    case_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[OneOffSignatureResponse]:
    return await OneOffSignaturesManager(db).list(agent, case_id)


@router.post("", response_model=OneOffSignatureResponse, status_code=201)
async def create_draft(
    case_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    file: UploadFile,
    name: Annotated[str, Form(min_length=1, max_length=100)],
    progress_id: Annotated[uuid.UUID, Form()],
    person_id: Annotated[uuid.UUID, Form()],
) -> OneOffSignatureResponse:
    limit = get_settings().max_document_size_mb * 1024 * 1024
    return await OneOffSignaturesManager(db).create(
        agent,
        case_id,
        name=name,
        progress_id=progress_id,
        person_id=person_id,
        filename=file.filename or "document.pdf",
        content=await file.read(limit + 1),
        content_type=file.content_type,
    )


@router.post("/{draft_id}/builder-token", response_model=BuilderTokenResponse)
async def builder_token(
    case_id: uuid.UUID, draft_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> BuilderTokenResponse:
    token = await OneOffSignaturesManager(db).builder_token(agent, case_id, draft_id)
    return BuilderTokenResponse(token=token, provider="docuseal")


@router.post("/{draft_id}/builder-sync", response_model=OneOffSignatureResponse)
async def builder_sync(
    case_id: uuid.UUID, draft_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> OneOffSignatureResponse:
    return await OneOffSignaturesManager(db).sync(agent, case_id, draft_id)


@router.delete("/{draft_id}", status_code=204)
async def delete_draft(case_id: uuid.UUID, draft_id: uuid.UUID, agent: AgentDep, db: DbDep) -> None:
    await OneOffSignaturesManager(db).delete(agent, case_id, draft_id)


@router.post("/{draft_id}/submit", response_model=OneOffSignatureResponse)
async def submit_draft(
    case_id: uuid.UUID, draft_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> OneOffSignatureResponse:
    return await OneOffSignaturesManager(db).submit(agent, case_id, draft_id)
