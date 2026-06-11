from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.dashboard.dashboard_manager import DashboardManager
from src.dashboard.dashboard_schema import DashboardResponse

router = APIRouter(tags=["dashboard"])

BINDINGS = [
    RouteBinding("GET", "/dashboard", Audience.AGENT, Permission.CASE_VIEW),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(agent: AgentDep, db: DbDep) -> DashboardResponse:
    return await DashboardManager(db).get_dashboard(agent)
