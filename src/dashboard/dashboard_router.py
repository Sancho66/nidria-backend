from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.i18n import RequestLang
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.dashboard.dashboard_manager import ActivityManager, DashboardManager, WorklistManager
from src.dashboard.dashboard_schema import (
    ActivityResponse,
    DashboardMeResponse,
    DashboardResponse,
    WorklistResponse,
)

router = APIRouter(tags=["dashboard"])

BINDINGS = [
    RouteBinding("GET", "/dashboard", Audience.AGENT, Permission.CASE_VIEW),
    # Agent-centric "dashboard of action" — scoped to the connected agent in
    # the manager (responsible/validator == me). case.view: any agent reads
    # its OWN actions.
    RouteBinding("GET", "/dashboard/me", Audience.AGENT, Permission.CASE_VIEW),
    # Unified "to handle" queue - same gate and same server-side
    # per-agent scoping as /dashboard/me.
    RouteBinding("GET", "/dashboard/worklist", Audience.AGENT, Permission.CASE_VIEW),
    # Client activity feed - same gate, agency-scoped in the query.
    RouteBinding("GET", "/dashboard/activity", Audience.AGENT, Permission.CASE_VIEW),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(agent: AgentDep, db: DbDep) -> DashboardResponse:
    return await DashboardManager(db).get_dashboard(agent)


@router.get("/dashboard/me", response_model=DashboardMeResponse)
async def get_my_dashboard(agent: AgentDep, db: DbDep, lang: RequestLang) -> DashboardMeResponse:
    return await DashboardManager(db).get_my_dashboard(agent, lang)


@router.get("/dashboard/worklist", response_model=WorklistResponse)
async def get_worklist(agent: AgentDep, db: DbDep, lang: RequestLang) -> WorklistResponse:
    """The agent's unified "to handle" queue: steps awaiting my
    validation, my late steps, client documents to review, reminders to
    approve on my cases - overdue first, oldest waiting first."""
    return await WorklistManager(db).get_worklist(agent, lang)


@router.get("/dashboard/activity", response_model=ActivityResponse)
async def get_activity(agent: AgentDep, db: DbDep) -> ActivityResponse:
    """The "Activite des clients" bento feed: agency-wide client
    gestures, aggregated per (type, case, day), 14 sliding days, 15
    items max, newest first. Demo excluded, agent gestures excluded."""
    return await ActivityManager(db).get_activity(agent)
