import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.impersonation.impersonation_manager import ImpersonationManager
from src.impersonation.impersonation_schema import ImpersonationTokenResponse

router = APIRouter(tags=["impersonation"])

BINDINGS = [
    RouteBinding(
        "POST",
        "/agencies/me/members/{agent_id}/impersonate",
        Audience.AGENT,
        Permission.AGENT_IMPERSONATE,
    ),
    RouteBinding(
        "POST",
        "/expat-users/{expat_user_id}/impersonate",
        Audience.AGENT,
        Permission.AGENT_IMPERSONATE,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.post(
    "/agencies/me/members/{agent_id}/impersonate", response_model=ImpersonationTokenResponse
)
async def impersonate_agent(
    agent_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> ImpersonationTokenResponse:
    return await ImpersonationManager(db).impersonate_agent(agent, agent_id)


@router.post("/expat-users/{expat_user_id}/impersonate", response_model=ImpersonationTokenResponse)
async def impersonate_expat(
    expat_user_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> ImpersonationTokenResponse:
    return await ImpersonationManager(db).impersonate_expat(agent, expat_user_id)
