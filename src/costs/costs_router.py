import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.auth.auth_schema import MessageResponse
from src.core.currencies import list_supported
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.costs.costs_manager import CostsManager
from src.costs.costs_schema import (
    CaseCostsResponse,
    CostLineCreateRequest,
    CostLineResponse,
    CostLineUpdateRequest,
    CurrencyResponse,
)

router = APIRouter(tags=["costs"])

_VIEW = Permission.COST_VIEW
_MANAGE = Permission.COST_MANAGE

# AGENT face ONLY. cost.view reads; cost.manage writes. No expat/external route
# ever carries these — a cost is structurally absent from those faces.
BINDINGS = [
    # Reference data — any authenticated agent (the currency selector in the
    # agency settings). No cost permission: choosing a currency is agency
    # profile work, not cost work.
    RouteBinding("GET", "/currencies", Audience.AGENT),
    RouteBinding("GET", "/cases/{case_id}/costs", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/cases/{case_id}/steps/{progress_id}/costs", Audience.AGENT, _MANAGE),
    RouteBinding("PATCH", "/cases/{case_id}/costs/{cost_id}", Audience.AGENT, _MANAGE),
    RouteBinding("DELETE", "/cases/{case_id}/costs/{cost_id}", Audience.AGENT, _MANAGE),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/currencies", response_model=list[CurrencyResponse])
async def list_currencies(agent: AgentDep) -> list[CurrencyResponse]:
    return [
        CurrencyResponse(code=c.code, name=c.name, decimals=c.decimals) for c in list_supported()
    ]


@router.get("/cases/{case_id}/costs", response_model=CaseCostsResponse)
async def list_costs(case_id: uuid.UUID, agent: AgentDep, db: DbDep) -> CaseCostsResponse:
    return await CostsManager(db).list_costs(agent, case_id)


@router.post(
    "/cases/{case_id}/steps/{progress_id}/costs",
    response_model=CostLineResponse,
    status_code=201,
)
async def add_cost(
    case_id: uuid.UUID,
    progress_id: uuid.UUID,
    body: CostLineCreateRequest,
    agent: AgentDep,
    db: DbDep,
) -> CostLineResponse:
    return await CostsManager(db).add_cost(agent, case_id, progress_id, body)


@router.patch("/cases/{case_id}/costs/{cost_id}", response_model=CostLineResponse)
async def update_cost(
    case_id: uuid.UUID,
    cost_id: uuid.UUID,
    body: CostLineUpdateRequest,
    agent: AgentDep,
    db: DbDep,
) -> CostLineResponse:
    return await CostsManager(db).update_cost(agent, case_id, cost_id, body)


@router.delete("/cases/{case_id}/costs/{cost_id}", response_model=MessageResponse)
async def delete_cost(
    case_id: uuid.UUID, cost_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await CostsManager(db).delete_cost(agent, case_id, cost_id)
    return MessageResponse(detail="Cost line deleted.")
