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
from src.admin.admin_router import router as admin_router
from src.agencies.agencies_router import public_router as agencies_public_router
from src.agencies.agencies_router import router as agencies_router
from src.auth.auth_router import router as auth_router
from src.billing.billing_router import router as billing_router
from src.cases.cases_router import router as cases_router
from src.comments.comments_router import agent_router as comments_agent_router
from src.comments.comments_router import expat_router as comments_expat_router
from src.consents.consents_router import router as consents_router
from src.consents.consents_seed import seed_consent_documents
from src.core.config import get_settings
from src.core.database import async_session_maker, get_db
from src.core.exceptions import register_exception_handlers
from src.core.rbac.baseline import collect_bindings, seed_bindings
from src.core.rbac.enforcement import enforce
from src.core.rbac.integrity import (
    assert_all_routes_bound,
    assert_impersonation_denylist_declared,
)
from src.core.rbac.permissions import sync_permissions
from src.core.scheduler import build_scheduler, make_session_local
from src.costs.costs_router import router as costs_router
from src.custom_fields.custom_fields_router import router as custom_fields_router
from src.dashboard.dashboard_router import router as dashboard_router
from src.documents.documents_router import agent_router as documents_agent_router
from src.documents.documents_router import expat_router as documents_expat_router
from src.expat.expat_router import router as expat_portal_router
from src.external.external_router import agency_router as external_agency_router
from src.external.external_router import external_router
from src.impersonation.impersonation_router import router as impersonation_router
from src.imports.imports_router import router as imports_router
from src.jobs.jobs_router import router as jobs_router
from src.journeys.journeys_router import router as journeys_router
from src.journeys.sample_seed import seed_sample_journeys
from src.journeys.sector_seed import seed_sector_templates
from src.profile.profile_router import router as profile_router
from src.progress.progress_router import router as progress_router
from src.reminders.reminders_router import router as reminders_router
from src.roles.roles_router import router as roles_router
from src.signup.signup_router import router as signup_router
from src.usage.usage_backfill import backfill_usage_milestones
from src.views.views_router import router as views_router

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

boot_logger = logging.getLogger("nidria.boot")
settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Boot order: mirror the permission catalogue into the DB,
    self-reconcile the route bindings from code (insert-missing only —
    the code is the source of truth, so a DB lagging the code never
    fails the boot for a mere seed gap), then refuse to start if any
    declared route STILL lacks a binding (a genuine code omission — deny
    by default made loud), then start the scheduler (job crons read from
    DB).

    The reconcile is strictly additive (see seed_bindings): a binding
    removed from code is NOT deleted from the DB, so assert_all_routes_bound
    keeps catching real orphans rather than silently papering over them."""
    async with async_session_maker() as session:
        await sync_permissions(session)
        await seed_bindings(session, collect_bindings())
        await assert_all_routes_bound(application, session)
        # Library samples (shared, read-only) — idempotent, like the system
        # roles. Agencies consume them by cloning.
        await seed_sample_journeys(session)
        # Sector library (7 global sector templates) — same pattern, keyed on
        # `sector`; cloned into an agency at creation (demo_case_seed).
        await seed_sector_templates(session)
        # Consent documents (point 16) — reconcile with the canonical
        # texts (consents_texts.py = source of truth): a text edited in
        # code publishes a NEW version at boot and re-gates everyone.
        await seed_consent_documents(session)
        # Usage trackers (bloc 1) — one-shot idempotent backfill: agencies
        # predating the event layer get their milestones from REAL data
        # (existing rows are never touched, no fake event is fabricated).
        await backfill_usage_milestones(session)
    # Paddle catalog check (LIGHT): do the env price_ids exist in Paddle and
    # carry their declared stable keys? Divergence → log ERROR, NEVER crash
    # (manual billing must survive a Paddle outage). Skipped when Paddle is
    # not configured (tests/CI: no key, no network — the app boots Paddle-less).
    settings_boot = get_settings()
    if not settings_boot.paddle_boot_check:
        # DEV opt-out (PADDLE_BOOT_CHECK=false in the local .env): each
        # uvicorn reload was one Paddle GET — a dev session got the sandbox
        # rate-limited (Cloudflare 429, 2026-07-17). The catalog does not
        # change between two file saves; prod (Fly) keeps the check.
        boot_logger.info("paddle catalog check: disabled by PADDLE_BOOT_CHECK")
    elif settings_boot.paddle_api_key and settings_boot.paddle_price_ids:
        try:
            from src.billing.catalog_provisioning import verify_catalog_env
            from src.billing.paddle_client import PaddleClient

            problems = await verify_catalog_env(
                client=PaddleClient(), price_ids=settings_boot.paddle_price_ids
            )
            for problem in problems:
                boot_logger.error("paddle catalog check: %s", problem)
            if not problems:
                boot_logger.info(
                    "paddle catalog check: %s price ids verified",
                    len(settings_boot.paddle_price_ids),
                )
        except Exception as exc:
            from src.billing.paddle_client import PaddleApiError

            # A rate-limit answers with a Cloudflare HTML page: recognize
            # it and log ONE clean line instead of 500 chars of IE6 HTML.
            # No retry at boot — the next deploy re-verifies.
            if isinstance(exc, PaddleApiError) and (
                exc.status_code == 429 or "cloudflare" in str(exc).lower()
            ):
                boot_logger.error(
                    "paddle catalog check: rate limited by Paddle, check skipped, will not retry"
                )
            else:
                boot_logger.exception("paddle catalog check failed (non-blocking)")
    elif not settings_boot.billing_checkout_enabled:
        # A closed prod without Paddle keys is a LEGITIMATE state today
        # (wired but shut until Eric opens the offer) — INFO, not ERROR.
        boot_logger.info(
            "paddle catalog check skipped: checkout disabled and Paddle not configured"
        )
    else:
        # Checkout OPEN but Paddle unconfigured: every checkout would 409 —
        # that one deserves the alert.
        boot_logger.error(
            "paddle catalog check: BILLING_CHECKOUT_ENABLED is true but Paddle "
            "keys/price ids are missing"
        )
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
app.include_router(signup_router)
app.include_router(billing_router)
app.include_router(activity_router)
app.include_router(admin_router)
app.include_router(agencies_router)
app.include_router(agencies_public_router)
# views BEFORE cases: GET /cases/columns (literal) must register ahead
# of GET /cases/{case_id} or "columns" 422s against the UUID parser.
app.include_router(views_router)
app.include_router(cases_router)
app.include_router(custom_fields_router)
app.include_router(dashboard_router)
app.include_router(documents_agent_router)
app.include_router(documents_expat_router)
app.include_router(comments_agent_router)
app.include_router(comments_expat_router)
app.include_router(consents_router)
app.include_router(costs_router)
app.include_router(external_router)
app.include_router(external_agency_router)
app.include_router(expat_portal_router)
app.include_router(impersonation_router)
app.include_router(imports_router)
app.include_router(jobs_router)
app.include_router(journeys_router)
app.include_router(profile_router)
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
    return JSONResponse(content={"status": "ok", "db": "ok"})
