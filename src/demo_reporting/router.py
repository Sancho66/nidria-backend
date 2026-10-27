import hashlib
import hmac
import re
from collections.abc import AsyncIterator
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.database import async_session_maker
from src.core.enums import Audience
from src.core.exceptions import UnauthorizedError
from src.core.rbac.baseline import RouteBinding
from src.demo_reporting.manager import DemoReportingManager
from src.demo_reporting.schema import DemoStatsResponse

router = APIRouter(prefix="/reports", tags=["demo-reporting"])
# PUBLIC means no agent/expat session. The dedicated dependency below is
# mandatory and validates a capability that grants only this one GET.
BINDINGS = [RouteBinding("GET", "/reports/demo-stats", Audience.PUBLIC)]
bearer = HTTPBearer(auto_error=False)


async def authorize_report(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    expected = get_settings().demo_stats_token_sha256
    supplied = credentials.credentials if credentials else ""
    if (
        not re.fullmatch(r"[a-f0-9]{64}", expected)
        or not 40 <= len(supplied) <= 200
        or not hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), expected)
    ):
        raise UnauthorizedError("Invalid reporting credential.")


async def report_db() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield db
        finally:
            await db.rollback()


@router.get(
    "/demo-stats", response_model=DemoStatsResponse, dependencies=[Depends(authorize_report)]
)
async def demo_stats(
    response: Response,
    db: Annotated[AsyncSession, Depends(report_db)],
    lot: str = Query(min_length=1, max_length=100),
    date: date | None = None,
) -> DemoStatsResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return await DemoReportingManager(db).read(lot, date)
