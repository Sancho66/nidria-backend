import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.activity.activity_manager import ActivityManager
from src.activity.activity_schema import ActivityListResponse
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(prefix="/cases", tags=["activity"])

BINDINGS = [
    RouteBinding("GET", "/cases/{case_id}/activity", Audience.AGENT, Permission.CASE_VIEW),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/{case_id}/activity", response_model=ActivityListResponse)
async def list_case_activity(
    case_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    action_type: Annotated[list[str] | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ActivityListResponse:
    return await ActivityManager(db).list_case_activity(
        agent, case_id, action_type, page, page_size
    )
