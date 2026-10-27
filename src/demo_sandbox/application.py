"""Serve existing agency/client reads against a separate, read-only database.

Run only with `uvicorn src.demo_sandbox.application:app`. This module is not
imported by `src.main`, so production gains no public authentication path.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from src.auth.auth_router import router as auth_router
from src.comments.comments_router import expat_router as comments_router
from src.consents.consents_router import router as consents_router
from src.core.config import get_settings
from src.core.database import async_session_maker, get_db
from src.core.dependencies import get_expat_token_payload
from src.core.exceptions import NotFoundError, register_exception_handlers
from src.core.rbac.enforcement import enforce
from src.core.rbac.integrity import assert_all_routes_bound
from src.demo_sandbox.agency_manager import AgencyDemoManager
from src.demo_sandbox.agency_repository import AgencyDemoRepository
from src.demo_sandbox.agency_routes import agency_read_router
from src.demo_sandbox.catalog import DemoCatalog, load_catalog
from src.demo_sandbox.control import control_application, control_db
from src.demo_sandbox.guards import SANDBOX_DB_NAME, assert_isolated_settings
from src.demo_sandbox.lifecycle import history, purge_expired
from src.demo_sandbox.manager import DemoManager
from src.demo_sandbox.schema import DemoSessionRequest, DemoSessionResponse
from src.demo_sandbox.storage import load_demo_documents
from src.demo_sandbox.visits import track_visit
from src.documents.documents_router import expat_router as documents_router
from src.expat.expat_router import router as expat_router
from src.signatures.signatures_router import router as signatures_router

# Reuse the real client handlers and schemas. No webhook, provider
# callback, password, consent acceptance, upload or other write route is mounted.
READ_ROUTES = frozenset(
    {
        "/auth/expat/me",
        "/expat/agencies/{agency_id}/logo",
        "/consents/expat/pending",
        "/expat/cases",
        "/expat/cases/{case_id}",
        "/expat/cases/{case_id}/notifications",
        "/expat/cases/{case_id}/documents",
        "/expat/cases/{case_id}/documents/{document_id}/download",
        "/expat/cases/{case_id}/signatures",
        "/expat/cases/{case_id}/steps/{progress_id}/comments",
    }
)


async def read_only_db() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield db
        finally:
            await db.rollback()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    assert_isolated_settings(get_settings())
    catalog: DemoCatalog = application.state.demo_catalog
    async with async_session_maker() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        await assert_all_routes_bound(application, db)
        database = await db.scalar(text("SELECT current_database()"))
        if database != SANDBOX_DB_NAME:
            raise RuntimeError("Unexpected sandbox database identity")
        # Runtime credentials cannot create, change or delete business data.
        unsafe_role = await db.scalar(
            text("""
            SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls
            FROM pg_roles WHERE rolname = current_user
        """)
        )
        if unsafe_role:
            raise RuntimeError("Sandbox reader has administrative database privileges")
        writable = await db.scalar(
            text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_tables WHERE schemaname = 'public'
                AND (has_table_privilege(current_user, quote_ident(schemaname) || '.' ||
                    quote_ident(tablename), 'INSERT,UPDATE,DELETE,TRUNCATE'))
            )
        """)
        )
        if writable:
            raise RuntimeError("Sandbox runtime database role is not read-only")
        counts = (
            await db.execute(
                text("""
            SELECT (SELECT count(*) FROM agency) AS agencies,
                   (SELECT count(*) FROM client_case) AS cases,
                   (SELECT count(*) FROM client_case WHERE is_demo) AS demos
        """)
            )
        ).one()
        if counts.cases != counts.demos:
            raise RuntimeError("Sandbox must contain only fictional agencies and cases")
        deleted_codes = set(await db.scalars(select(history.c.code)))
        for record in catalog.records:
            if record.public_code in deleted_codes:
                continue
            if (
                await DemoManager(db, catalog).repo.resolve(
                    record, catalog.fingerprint, allow_disabled=True
                )
                is None
            ):
                raise RuntimeError("Sandbox seed is incomplete or differs from the catalog")
        # Keyset pages keep startup memory and database round-trips bounded
        # after thousands of imports. All isolation checks still run.
        after = None
        while True:
            query = select(Agency).order_by(Agency.id).limit(100)
            if after is not None:
                query = query.where(Agency.id > after)
            agencies = list(await db.scalars(query))
            if not agencies:
                break
            full = [agency for agency in agencies if agency.settings.get("sandbox_full_demo")]
            if not await AgencyDemoRepository(db).valid_population(full):
                raise RuntimeError("Invalid fictional agency demo")
            for agency in agencies:
                if agency in full:
                    continue
                if agency.settings.get("sandbox_dynamic_entry"):
                    from src.demo_sandbox.catalog import DemoRecord
                    from src.demo_sandbox.repository import record_fingerprint

                    record = DemoRecord.model_validate(agency.settings["sandbox_dynamic_entry"])
                    if (
                        await DemoManager(db, catalog).repo.resolve(
                            record, record_fingerprint(record), allow_disabled=True
                        )
                        is None
                    ):
                        raise RuntimeError("Invalid dynamic sandbox demo")
                elif catalog.by_id(agency.settings.get("sandbox_record", "")) is None:
                    raise RuntimeError("Unexpected agency in the isolated sandbox")
            after = agencies[-1].id
        load_demo_documents(catalog)
        await db.rollback()

    async def purge_loop() -> None:
        while True:
            try:
                async for writer in control_db():
                    removed = await purge_expired(writer, catalog)
                # Drain large expired batches gradually, without keeping a
                # long transaction open or calling the production scheduler.
                await asyncio.sleep(5 if removed == 100 else 3600)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("Isolated demo expiry sweep failed")
                await asyncio.sleep(300)

    from src.demo_sandbox.import_manager import import_loop

    tasks = (
        [asyncio.create_task(purge_loop()), asyncio.create_task(import_loop(catalog))]
        if get_settings().demo_admin_enabled
        else []
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


def create_sandbox_app(catalog: DemoCatalog | None = None) -> FastAPI:
    application = FastAPI(
        title="Nidria isolated demo entry",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.demo_catalog = catalog or load_catalog()
    application.dependency_overrides[get_db] = read_only_db
    register_exception_handlers(application)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Accept-Language"],
    )

    @application.middleware("http")
    async def no_cache(request: Request, call_next: Any) -> Any:
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not (
            request.method == "POST"
            and request.url.path in {"/demo/session", "/_control/demos", "/_control/stats"}
        ):
            response = JSONResponse(
                {"detail": "This demo is read-only.", "code": "impersonation.read_only"},
                status_code=403,
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "environment": "demo_sandbox"}

    @application.post(
        "/demo/session", response_model=DemoSessionResponse, dependencies=[Depends(enforce)]
    )
    async def session(
        body: DemoSessionRequest, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
    ) -> DemoSessionResponse:
        agency = await AgencyDemoManager(db).open_session(body)
        result = agency or await DemoManager(db, application.state.demo_catalog).open_session(
            body.code
        )
        from src.demo_sandbox.measurement import demo_attribution, excluded_reason

        result.attribution_id = demo_attribution(body.code)
        if body.visit_id is not None:
            await track_visit(
                body.code,
                body.visit_id,
                body.tour,
                trial_clicked=body.trial_clicked,
                reason=excluded_reason(
                    body.admin_preview, body.automated, request.headers.get("User-Agent", "")
                ),
            )
        return result

    async def scope(
        request: Request,
        payload: Annotated[dict[str, Any], Depends(get_expat_token_payload)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> None:
        identity = (
            await AgencyDemoManager(db).verify_expat(payload)
            if payload.get("demo_agency_id")
            else await DemoManager(db, application.state.demo_catalog).verify_session(payload)
        )
        requested_case = request.path_params.get("case_id")
        if requested_case is not None and str(requested_case) != str(identity.case.id):
            raise NotFoundError("Case not found.", code="case.not_found")

    selected = APIRouter()
    for source in (
        auth_router,
        consents_router,
        expat_router,
        documents_router,
        signatures_router,
        comments_router,
    ):
        selected.routes.extend(
            route
            for route in source.routes
            if isinstance(route, APIRoute)
            and route.path in READ_ROUTES
            and route.methods == {"GET"}
        )
    if {route.path for route in selected.routes if isinstance(route, APIRoute)} != READ_ROUTES:
        raise RuntimeError("The production client portal contract has changed")
    application.include_router(selected, dependencies=[Depends(scope), Depends(enforce)])
    application.include_router(agency_read_router())
    application.mount("/_control", control_application(application.state.demo_catalog))
    return application


app = create_sandbox_app()
