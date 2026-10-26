"""Authenticated platform operators manage demos through an isolated service."""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query

from src.admin.admin_router import AgentDep
from src.admin.demo_manager import AdminDemoManager
from src.admin.demo_schema import (
    AgencyDemoBatchRequest,
    AgencyDemoBatchResponse,
    DemoBulkRequest,
    DemoBulkResponse,
    DemoCreateRequest,
    DemoExportResponse,
    DemoFilters,
    DemoJourneyInput,
    DemoJourneyPreview,
    DemoListResponse,
    DemoRead,
    DemoSelection,
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
        ("POST", "/admin/demos/agency-batch"),
        ("GET", "/admin/demos/templates"),
        ("POST", "/admin/demos/preview"),
        ("POST", "/admin/demos/{code}/disable"),
        ("POST", "/admin/demos/export"),
        ("POST", "/admin/demos/bulk"),
        ("GET", "/admin/demos/archive"),
    )
]


@router.get("", response_model=DemoListResponse)
async def list_demos(
    agent: AgentDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    search: str = Query("", max_length=200),
    batch: str = Query("", max_length=100),
    status: Literal["all", "active", "disabled", "expired"] = "all",
    opened: Literal["all", "opened", "unopened"] = "all",
) -> DemoListResponse:
    return await AdminDemoManager().list_demos(
        agent.id,
        page,
        page_size,
        search,
        DemoFilters(search=search, batch=batch, status=status, opened=opened),
    )


@router.post("/export", response_model=DemoExportResponse)
async def export_demos(payload: DemoSelection, agent: AgentDep) -> DemoExportResponse:
    return await AdminDemoManager().export_selection(agent.id, payload)


@router.post("/bulk", response_model=DemoBulkResponse)
async def bulk_demos(payload: DemoBulkRequest, agent: AgentDep) -> DemoBulkResponse:
    return await AdminDemoManager().bulk(agent.id, payload)


@router.get("/archive", response_model=DemoExportResponse)
async def demo_archive(
    agent: AgentDep, batch: str = Query("", max_length=100)
) -> DemoExportResponse:
    return await AdminDemoManager().archive(agent.id, batch)


@router.get("/templates", response_model=list[DemoTemplateRead])
async def demo_templates(agent: AgentDep) -> list[DemoTemplateRead]:
    return await AdminDemoManager().templates(agent.id)


@router.post("/agency-batch", response_model=AgencyDemoBatchResponse, status_code=201)
async def create_agency_batch(
    payload: AgencyDemoBatchRequest, agent: AgentDep
) -> AgencyDemoBatchResponse:
    return await AdminDemoManager().create_agency_batch(agent.id, payload)


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
