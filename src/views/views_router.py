"""Saved views — ported from Prism (src/views/views_router). URL
adaptations only (no {slug}: tenancy is token-based): /views…, columns
catalog at GET /cases/columns. Declaration order preserved from Prism:
the /default-all routes MUST stay above /{view_id} — matched later,
the literal path segment would 422 against the UUID parser."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.views.views_manager import ViewsManager
from src.views.views_schema import (
    AvailableColumnsResponse,
    SavedViewCreate,
    SavedViewDefaultAllUpdate,
    SavedViewRead,
    SavedViewUpdate,
)

router = APIRouter(tags=["views"])

# Auth-only spirit of Prism (a viewer customizes their own display):
# everything gates on the broadest case permission, case.view.
_VIEW = Permission.CASE_VIEW

BINDINGS = [
    RouteBinding("GET", "/cases/columns", Audience.AGENT, _VIEW),
    RouteBinding("GET", "/views", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/views", Audience.AGENT, _VIEW),
    RouteBinding("GET", "/views/default-all", Audience.AGENT, _VIEW),
    RouteBinding("PUT", "/views/default-all", Audience.AGENT, _VIEW),
    RouteBinding("DELETE", "/views/default-all", Audience.AGENT, _VIEW),
    RouteBinding("PATCH", "/views/{view_id}", Audience.AGENT, _VIEW),
    RouteBinding("DELETE", "/views/{view_id}", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/views/{view_id}/set-default", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/views/{view_id}/unset-default", Audience.AGENT, _VIEW),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/cases/columns", response_model=AvailableColumnsResponse)
async def list_available_columns(agent: AgentDep, db: DbDep) -> AvailableColumnsResponse:
    """The columns the cases list can render: `default` (visible in the
    no-view state), `locked` (cannot be hidden), `type` (render hint)."""
    return ViewsManager(db).list_available_columns()


@router.get("/views", response_model=list[SavedViewRead])
async def list_views(
    agent: AgentDep,
    db: DbDep,
    entity: Annotated[str | None, Query(description="Filter to one entity ('cases')")] = None,
) -> list[SavedViewRead]:
    return await ViewsManager(db).list_(agent, entity)


@router.post("/views", response_model=SavedViewRead, status_code=status.HTTP_201_CREATED)
async def create_view(request: SavedViewCreate, agent: AgentDep, db: DbDep) -> SavedViewRead:
    return await ViewsManager(db).create(agent, request)


# --- Customizable "All" view (Prism order: BEFORE /{view_id}) ----------------


@router.get(
    "/views/default-all",
    response_model=SavedViewRead,
    responses={204: {"description": "No customized 'All' exists for this entity yet."}},
)
async def get_default_all_view(
    entity: Annotated[str, Query(description="One of: cases_all")],
    agent: AgentDep,
    db: DbDep,
) -> SavedViewRead | Response:
    """204 (not 404) when none exists — absence is a normal state: the
    agent simply hasn't customized the tab, the frontend renders the
    pristine zero-filter "All"."""
    result = await ViewsManager(db).get_default_all(agent, entity)
    if result is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return result


@router.put("/views/default-all", response_model=SavedViewRead)
async def upsert_default_all_view(
    request: SavedViewDefaultAllUpdate,
    entity: Annotated[str, Query(description="One of: cases_all")],
    agent: AgentDep,
    db: DbDep,
) -> SavedViewRead:
    """Save-from-"All": create-or-update the caller's customized "All"."""
    return await ViewsManager(db).upsert_default_all(agent, entity, request)


@router.delete("/views/default-all", status_code=status.HTTP_204_NO_CONTENT)
async def reset_default_all_view(
    entity: Annotated[str, Query(description="One of: cases_all")],
    agent: AgentDep,
    db: DbDep,
) -> Response:
    """Reset back to the pristine zero-filter "All". Idempotent."""
    await ViewsManager(db).reset_default_all(agent, entity)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/views/{view_id}", response_model=SavedViewRead)
async def update_view(
    view_id: uuid.UUID, request: SavedViewUpdate, agent: AgentDep, db: DbDep
) -> SavedViewRead:
    return await ViewsManager(db).update(agent, view_id, request)


@router.delete("/views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_view(view_id: uuid.UUID, agent: AgentDep, db: DbDep) -> Response:
    await ViewsManager(db).delete(agent, view_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/views/{view_id}/set-default", response_model=SavedViewRead)
async def set_default_view(view_id: uuid.UUID, agent: AgentDep, db: DbDep) -> SavedViewRead:
    return await ViewsManager(db).set_default(agent, view_id)


@router.post("/views/{view_id}/unset-default", response_model=SavedViewRead)
async def unset_default_view(view_id: uuid.UUID, agent: AgentDep, db: DbDep) -> SavedViewRead:
    return await ViewsManager(db).unset_default(agent, view_id)
