from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from src.consents.consents_manager import ConsentsManager
from src.consents.consents_schema import (
    ConsentAcceptRequest,
    ConsentAcceptResponse,
    ExpatAgencyPendingResponse,
    PendingDocumentResponse,
)
from src.core.dependencies import get_current_agent, get_current_expat, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding

router = APIRouter(prefix="/consents", tags=["consents"])

# No permission on the agent bindings: consent is an IDENTITY concern
# (like /me, /logout), not a matrix one. All four routes are also in
# enforcement.CONSENT_EXEMPT: they must stay reachable BEFORE consent.
BINDINGS = [
    RouteBinding("GET", "/consents/agent/pending", Audience.AGENT),
    RouteBinding("POST", "/consents/agent/accept", Audience.AGENT),
    RouteBinding("GET", "/consents/expat/pending", Audience.EXPAT),
    RouteBinding("POST", "/consents/expat/accept", Audience.EXPAT),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]
ExpatDep = Annotated[ExpatUser, Depends(get_current_expat)]


def _client_ip(request: Request) -> str | None:
    """First X-Forwarded-For hop (the original client, behind Fly's
    proxy), else the direct peer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/agent/pending", response_model=list[PendingDocumentResponse])
async def agent_pending(agent: AgentDep, db: DbDep) -> list[PendingDocumentResponse]:
    """Active documents the agent still has to accept (empty for any
    agent that is not the agency admin)."""
    return await ConsentsManager(db).pending_for_agent(agent)


@router.post("/agent/accept", response_model=ConsentAcceptResponse)
async def agent_accept(
    body: ConsentAcceptRequest, request: Request, agent: AgentDep, db: DbDep
) -> ConsentAcceptResponse:
    return await ConsentsManager(db).accept_as_agent(agent, body, _client_ip(request))


@router.get("/expat/pending", response_model=list[ExpatAgencyPendingResponse])
async def expat_pending(expat: ExpatDep, db: DbDep) -> list[ExpatAgencyPendingResponse]:
    """Documents still to accept, grouped PER AGENCY holding a live case
    of this client (the agency is the data controller)."""
    return await ConsentsManager(db).pending_for_expat(expat)


@router.post("/expat/accept", response_model=ConsentAcceptResponse)
async def expat_accept(
    body: ConsentAcceptRequest, request: Request, expat: ExpatDep, db: DbDep
) -> ConsentAcceptResponse:
    return await ConsentsManager(db).accept_as_expat(expat, body, _client_ip(request))
