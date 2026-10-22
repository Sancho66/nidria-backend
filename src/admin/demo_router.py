"""Authenticated platform operators manage demos through an isolated service."""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from src.admin.admin_router import AgentDep
from src.admin.demo_manager import AdminDemoManager
from src.admin.demo_schema import (
    DemoCreateRequest,
    DemoJourneyInput,
    DemoJourneyPreview,
    DemoListResponse,
    DemoRead,
    DemoTemplateRead,
)
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(prefix="/admin/demos", tags=["admin-demos"])
BINDINGS = [
    RouteBinding(method, path, Audience.AGENT, Permission.AGENCY_CREATE)
    for method, path in (
        ("GET", "/admin/demos"),
        ("POST", "/admin/demos"),
        ("GET", "/admin/demos/templates"),
        ("POST", "/admin/demos/preview"),
        ("POST", "/admin/demos/{code}/disable"),
    )
]


@router.get("", response_model=DemoListResponse)
async def list_demos(
    agent: AgentDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    search: str = Query("", max_length=200),
) -> DemoListResponse:
    return await AdminDemoManager().list_demos(agent.id, page, page_size, search)


@router.get("/templates", response_model=list[DemoTemplateRead])
async def demo_templates(agent: AgentDep) -> list[DemoTemplateRead]:
    return await AdminDemoManager().templates(agent.id)


@router.post("/preview", response_model=DemoJourneyPreview)
async def preview_demo(payload: DemoJourneyInput, agent: AgentDep) -> DemoJourneyPreview:
    return await AdminDemoManager().preview(agent.id, payload)


@router.post("", response_model=DemoRead, status_code=201)
async def create_demo(payload: DemoCreateRequest, agent: AgentDep) -> DemoRead:
    return await AdminDemoManager().create(agent.id, payload)


@router.post("/{code}/disable", response_model=DemoRead)
async def disable_demo(
    code: Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")], agent: AgentDep
) -> DemoRead:
    return await AdminDemoManager().disable(agent.id, code)
