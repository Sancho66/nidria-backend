"""Support scope is additive; existing agency/client auth stays unchanged."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.http import file_download_response
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.support_access.manager import SupportAccessManager
from src.support_access.schema import (
    SupportAccessResponse,
    SupportCaseContentResponse,
    SupportCasesResponse,
    SupportGrantCreate,
    SupportGrantResponse,
    SupportOperatorResponse,
)

router = APIRouter(tags=["support-access"])
BINDINGS = [
    RouteBinding("GET", "/support/operators", Audience.AGENT, Permission.AGENCY_MANAGE),
    RouteBinding("GET", "/support/cases", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("GET", "/support/cases/{case_id}/content", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding(
        "GET",
        "/support/cases/{case_id}/documents/{document_id}/download",
        Audience.AGENT,
        Permission.CASE_VIEW,
    ),
    RouteBinding(
        "GET", "/cases/{case_id}/support-access", Audience.AGENT, Permission.AGENCY_MANAGE
    ),
    RouteBinding(
        "POST", "/cases/{case_id}/support-access", Audience.AGENT, Permission.AGENCY_MANAGE
    ),
    RouteBinding(
        "DELETE",
        "/cases/{case_id}/support-access/{grant_id}",
        Audience.AGENT,
        Permission.AGENCY_MANAGE,
    ),
]
DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


def _impersonated(request: Request) -> bool:
    return getattr(request.state, "impersonator_id", None) is not None


def _operator(request: Request) -> uuid.UUID | None:
    value = getattr(request.state, "support_operator_id", None)
    return uuid.UUID(str(value)) if value is not None else None


@router.get("/support/operators", response_model=list[SupportOperatorResponse])
async def operators(request: Request, agent: AgentDep, db: DbDep) -> list[SupportOperatorResponse]:
    return await SupportAccessManager(db).operators(agent, _impersonated(request))


@router.get("/support/cases", response_model=SupportCasesResponse)
async def overview(
    request: Request,
    agent: AgentDep,
    db: DbDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> SupportCasesResponse:
    return await SupportAccessManager(db).overview(agent, _operator(request), page, page_size)


@router.get("/support/cases/{case_id}/content", response_model=SupportCaseContentResponse)
async def content(
    case_id: uuid.UUID, request: Request, response: Response, agent: AgentDep, db: DbDep
) -> SupportCaseContentResponse:
    response.headers["Cache-Control"] = "no-store, private"
    return await SupportAccessManager(db).content(agent, _operator(request), case_id)


@router.get("/support/cases/{case_id}/documents/{document_id}/download")
async def download(
    case_id: uuid.UUID, document_id: uuid.UUID, request: Request, agent: AgentDep, db: DbDep
) -> Response:
    filename, content = await SupportAccessManager(db).download(
        agent, _operator(request), case_id, document_id
    )
    response = file_download_response(filename, content)
    response.headers["Cache-Control"] = "no-store, private"
    return response


@router.get("/cases/{case_id}/support-access", response_model=SupportAccessResponse)
async def journal(
    case_id: uuid.UUID,
    request: Request,
    agent: AgentDep,
    db: DbDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> SupportAccessResponse:
    return await SupportAccessManager(db).journal(
        agent, case_id, _impersonated(request), page, page_size
    )


@router.post(
    "/cases/{case_id}/support-access", response_model=SupportGrantResponse, status_code=201
)
async def grant(
    case_id: uuid.UUID, body: SupportGrantCreate, request: Request, agent: AgentDep, db: DbDep
) -> SupportGrantResponse:
    return await SupportAccessManager(db).grant(agent, case_id, body, _impersonated(request))


@router.delete("/cases/{case_id}/support-access/{grant_id}", status_code=204)
async def revoke(
    case_id: uuid.UUID, grant_id: uuid.UUID, request: Request, agent: AgentDep, db: DbDep
) -> None:
    await SupportAccessManager(db).revoke(agent, case_id, grant_id, _impersonated(request))
