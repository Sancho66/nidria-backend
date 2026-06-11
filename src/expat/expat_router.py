import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.expat_user import ExpatUser
from src.core.dependencies import get_current_expat, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.expat.expat_manager import ExpatPortalManager
from src.expat.expat_schema import (
    ExpatCaseDetailResponse,
    ExpatCaseSummaryResponse,
    ExpatNotificationResponse,
)

router = APIRouter(prefix="/expat", tags=["expat-portal"])

BINDINGS = [
    RouteBinding("GET", "/expat/cases", Audience.EXPAT),
    RouteBinding("GET", "/expat/cases/{case_id}", Audience.EXPAT),
    RouteBinding("GET", "/expat/cases/{case_id}/notifications", Audience.EXPAT),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
ExpatDep = Annotated[ExpatUser, Depends(get_current_expat)]


@router.get("/cases", response_model=list[ExpatCaseSummaryResponse])
async def list_my_cases(expat: ExpatDep, db: DbDep) -> list[ExpatCaseSummaryResponse]:
    return await ExpatPortalManager(db).list_my_cases(expat)


@router.get("/cases/{case_id}", response_model=ExpatCaseDetailResponse)
async def get_my_case(case_id: uuid.UUID, expat: ExpatDep, db: DbDep) -> ExpatCaseDetailResponse:
    return await ExpatPortalManager(db).get_my_case(expat, case_id)


@router.get("/cases/{case_id}/notifications", response_model=list[ExpatNotificationResponse])
async def list_notifications(
    case_id: uuid.UUID, expat: ExpatDep, db: DbDep
) -> list[ExpatNotificationResponse]:
    return await ExpatPortalManager(db).list_notifications(expat, case_id)
