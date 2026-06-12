import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.activity_router import router as activity_router
from src.agencies.agencies_router import router as agencies_router
from src.auth.auth_router import router as auth_router
from src.cases.cases_router import router as cases_router
from src.core.config import get_settings
from src.core.database import async_session_maker, get_db
from src.core.exceptions import register_exception_handlers
from src.core.rbac.enforcement import enforce
from src.core.rbac.integrity import (
    assert_all_routes_bound,
    assert_impersonation_denylist_declared,
)
from src.core.rbac.permissions import sync_permissions
from src.core.scheduler import build_scheduler, make_session_local
from src.dashboard.dashboard_router import router as dashboard_router
from src.documents.documents_router import agent_router as documents_agent_router
from src.documents.documents_router import expat_router as documents_expat_router
from src.expat.expat_router import router as expat_portal_router
from src.impersonation.impersonation_router import router as impersonation_router
from src.jobs.jobs_router import router as jobs_router
from src.journeys.journeys_router import router as journeys_router
from src.progress.progress_router import router as progress_router
from src.reminders.reminders_router import router as reminders_router
from src.roles.roles_router import router as roles_router

# Configure logging so our INFO records actually print. uvicorn only
# configures its own loggers (`uvicorn`, `uvicorn.access`,
# `uvicorn.error`) — without a root handler, every
# `logging.getLogger("src.foo")` record propagates up to root, finds
# nothing, and is dropped silently. basicConfig installs the root
# handler; `force=True` lets us re-apply on `--reload` cycles without
# piling up duplicate handlers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    force=True,
)
# sqlalchemy.engine installs its own handler when echo=True; stop it
# from also propagating to root, which would print every SQL line twice.
sqla_logger = logging.getLogger("sqlalchemy.engine")
sqla_logger.propagate = False

settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Boot order: mirror the permission catalogue into the DB, refuse
    to start if any declared route lacks a binding (deny by default
    made loud), then start the scheduler (job crons read from DB)."""
    async with async_session_maker() as session:
        await sync_permissions(session)
        await assert_all_routes_bound(application, session)
    assert_impersonation_denylist_declared(application)
    application.state.sync_session_local = make_session_local()
    scheduler = None
    if settings.scheduler_enabled:
        scheduler = build_scheduler(application.state.sync_session_local)
        scheduler.start()
        application.state.scheduler = scheduler
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Nidria API",
    version="0.1.0",
    lifespan=lifespan,
    # Global enforcement: every path operation goes through the RBAC
    # engine; the infra whitelist is handled inside enforce itself.
    dependencies=[Depends(enforce)],
)

register_exception_handlers(app)

app.include_router(auth_router)
app.include_router(activity_router)
app.include_router(agencies_router)
app.include_router(cases_router)
app.include_router(dashboard_router)
app.include_router(documents_agent_router)
app.include_router(documents_expat_router)
app.include_router(expat_portal_router)
app.include_router(impersonation_router)
app.include_router(jobs_router)
app.include_router(journeys_router)
app.include_router(progress_router)
app.include_router(reminders_router)
app.include_router(roles_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ping")
async def ping() -> str:
    return "pong"


@app.get("/health")
async def health(db: Annotated[AsyncSession, Depends(get_db)]) -> JSONResponse:
    """Readiness probe for fly.io's HTTP healthcheck.

    Actively touches the DB (single `SELECT 1`) so a frozen pool or a
    dead Supabase connection surfaces as 503 instead of a 200 from a
    process that can't actually serve requests. Fly removes the
    machine from the LB on sustained failures and restarts it.
    """
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — boundary, want the message in the 503 body
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "db": str(exc)[:200]},
        )
    return JSONResponse(content={"status": "ok", "db": "ok", "version": settings.app_version})
