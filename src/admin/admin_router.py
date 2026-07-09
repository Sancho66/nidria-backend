"""Superadmin platform admin surface (Groupe C UI). ADDITIVE: no
existing endpoint is touched. GET /agencies stays the LIGHT switcher
endpoint (the header "Changer d'agence"); GET /admin/agencies is the
RICH table endpoint (the "Gérer les agences" screen). Same superadmin
gate as the rest of the platform lifecycle (agency.create)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.admin.admin_manager import AdminManager
from src.admin.admin_schema import AdminAgenciesResponse
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(prefix="/admin", tags=["admin"])

BINDINGS = [
    # Platform tool: only the superadmin holds agency.create; an agency
    # admin/agent/expat is 403. Reached with the superadmin's OWN token
    # (not an impersonation session — see the impersonation note in the
    # tests).
    RouteBinding("GET", "/admin/agencies", Audience.AGENT, Permission.AGENCY_CREATE),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/agencies", response_model=AdminAgenciesResponse)
async def list_agencies(
    agent: AgentDep,
    db: DbDep,
    search: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    trial_expiring_within_days: int | None = Query(None, ge=0),
    onboarding_incomplete: bool = False,
) -> AdminAgenciesResponse:
    """The superadmin agencies table: paginated, searchable (name/slug),
    sortable (created_at|name|cases_count), with derived status, seat/case
    counts, the 3 onboarding gestures, the S0/S1/S2 state and the login
    heartbeat. Filters (combinable, applied in SQL BEFORE pagination):
    `trial_expiring_within_days`, `onboarding_incomplete` — Eric's "who expires
    soon and hasn't started". A CONSTANT number of queries, no N+1."""
    return await AdminManager(db).list_agencies(
        search=search,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
        trial_expiring_within_days=trial_expiring_within_days,
        onboarding_incomplete=onboarding_incomplete,
    )
