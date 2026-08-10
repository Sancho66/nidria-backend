from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.export.export_manager import ExportManager

router = APIRouter(prefix="/agencies", tags=["export"])

BINDINGS = [
    # « Partir avec ses données » — a full agency dump is an administration
    # gesture (Settings), gated agency.manage (admin-only). A GET, so it
    # crosses the billing wall by construction: the agency leaves with its
    # data even when the subscription is blocked (that is the point).
    RouteBinding("GET", "/agencies/me/export", Audience.AGENT, Permission.AGENCY_MANAGE),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/me/export")
async def export_agency_data(agent: AgentDep, db: DbDep) -> Response:
    """Export the agency's profiles + cases + history as a ZIP of CSVs.
    Deposited documents are excluded (README says so); they stay
    downloadable case by case."""
    content, filename = await ExportManager(db).build_agency_export(agent)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
