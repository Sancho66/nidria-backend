"""Private management channel. Never accepts production authentication tokens."""

import hmac
import re
import time
import uuid
from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from src.admin.demo_schema import (
    AgencyDemoBatchRequest,
    DemoArchiveRequest,
    DemoBulkRequest,
    DemoControlEnvelope,
    DemoCreateRequest,
    DemoFilters,
    DemoJourneyInput,
    DemoSelection,
)
from src.admin.demo_transport import (
    CONTROL_PATH,
    MAX_BODY_BYTES,
    MAX_IMPORT_BODY_BYTES,
    STATS_PATH,
    signature,
)
from src.core.config import get_settings
from src.core.database import async_session_maker
from src.core.exceptions import ForbiddenError, ValidationError, register_exception_handlers
from src.demo_sandbox.catalog import DemoCatalog
from src.demo_sandbox.control_manager import DemoControlManager
from src.demo_sandbox.guards import SANDBOX_DB_HOSTS, SANDBOX_DB_NAME, assert_isolated_settings


class ListInput(DemoFilters):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=100)


@lru_cache
def writer_engine() -> AsyncEngine:
    settings = get_settings()
    assert_isolated_settings(settings)
    url = make_url(settings.demo_writer_database_url)
    if (
        url.host not in SANDBOX_DB_HOSTS
        or url.database != SANDBOX_DB_NAME
        or url.username != "nidria_demo_writer"
    ):
        raise RuntimeError("Invalid isolated demo writer destination")
    return create_async_engine(
        url, pool_pre_ping=True, pool_size=2, max_overflow=1, hide_parameters=True
    )


async def control_db() -> AsyncGenerator[AsyncSession, None]:
    # The outer connection transaction makes all reused managers atomic, even
    # though each manager commits its own session (SQLAlchemy create_savepoint).
    async with writer_engine().connect() as connection, connection.begin():
        database = await connection.scalar(text("SELECT current_database()"))
        unsafe = await connection.scalar(
            text(
                "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls "
                "FROM pg_roles WHERE rolname=current_user"
            )
        )
        if database != SANDBOX_DB_NAME or unsafe:
            raise RuntimeError("Unsafe isolated demo writer")
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as db:
            yield db
            await db.commit()


async def authorize(request: Request) -> uuid.UUID:
    settings = get_settings()
    if not settings.demo_admin_enabled or len(settings.demo_control_secret) < 48:
        raise ForbiddenError("Demo management is disabled.")
    timestamp = request.headers.get("X-Demo-Time", "")
    actor = request.headers.get("X-Demo-Actor", "")
    supplied = request.headers.get("X-Demo-Signature", "")
    try:
        actor_id = uuid.UUID(actor)
        fresh = abs(time.time() - int(timestamp)) <= 60
    except (ValueError, OverflowError) as exc:
        raise ForbiddenError("Invalid management signature.") from exc
    body = await request.body()
    if (
        not fresh
        or request.url.path not in {CONTROL_PATH, STATS_PATH}
        or len(body) > MAX_IMPORT_BODY_BYTES
        or not hmac.compare_digest(
            supplied,
            signature(settings.demo_control_secret, timestamp, actor, body, request.url.path),
        )
    ):
        raise ForbiddenError("Invalid management signature.")
    return actor_id


async def stats_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield db
        finally:
            await db.rollback()


def control_application(catalog: DemoCatalog) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    register_exception_handlers(app)

    @app.exception_handler(PydanticValidationError)
    async def invalid_payload(request: Request, exc: PydanticValidationError) -> Any:
        # Never echo a pasted journey or logo back in a validation response.
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"detail": "Invalid demo input.", "code": "demo.invalid_input", "params": {}},
            status_code=422,
        )

    @app.post("/stats", dependencies=[Depends(authorize)])
    async def stats(
        envelope: DemoControlEnvelope,
        db: Annotated[AsyncSession, Depends(stats_db)],
    ) -> Any:
        from src.demo_reporting.schema import DemoStatsQuery
        from src.demo_sandbox.stats import DemoStatsManager

        if envelope.operation != "stats":
            raise ForbiddenError("This channel only reads demo statistics.")
        return await DemoStatsManager(db).read(DemoStatsQuery.model_validate(envelope.payload))

    @app.post("/demos")
    async def dispatch(
        request: Request,
        actor: Annotated[uuid.UUID, Depends(authorize)],
        db: Annotated[AsyncSession, Depends(control_db, scope="function")],
        envelope: DemoControlEnvelope,
    ) -> Any:
        if len(await request.body()) > MAX_BODY_BYTES and envelope.operation != "import_start":
            raise ValidationError("Demo input is too large.", code="demo.input_too_large")
        manager = DemoControlManager(db, catalog)
        payload = envelope.payload
        match envelope.operation:
            case (
                "import_start"
                | "import_list"
                | "import_get"
                | "import_export"
                | "import_export_batch"
            ):
                from src.admin.demo_import_schema import DemoImportLookup, DemoImportRequest
                from src.demo_sandbox.import_manager import DemoImportManager

                imports = DemoImportManager(db)
                if envelope.operation == "import_start":
                    return await imports.start(actor, DemoImportRequest.model_validate(payload))
                if envelope.operation == "import_list":
                    return await imports.recent()
                if envelope.operation == "import_export_batch":
                    return await imports.export_batch(
                        DemoArchiveRequest.model_validate(payload).batch
                    )
                import_query = DemoImportLookup.model_validate(payload)
                if envelope.operation == "import_export":
                    return await imports.export(import_query.job_id, import_query.kind)
                return await imports.read(import_query.job_id)
            case "agency_batch":
                from src.demo_sandbox.agency_control import AgencyDemoControlManager

                return await AgencyDemoControlManager(db, catalog).create_batch(
                    actor, AgencyDemoBatchRequest.model_validate(payload)
                )
            case "list":
                query = ListInput.model_validate(payload)
                return await manager.list_demos(query.page, query.page_size, query.search, query)
            case "export":
                return await manager.export_selection(actor, DemoSelection.model_validate(payload))
            case "bulk":
                return await manager.bulk(actor, DemoBulkRequest.model_validate(payload))
            case "archive":
                from src.demo_sandbox.lifecycle import archived_export

                return await archived_export(db, DemoArchiveRequest.model_validate(payload).batch)
            case "templates":
                return manager.templates()
            case "preview":
                return await manager.preview(DemoJourneyInput.model_validate(payload))
            case "create":
                return await manager.create(actor, DemoCreateRequest.model_validate(payload))
            case "disable":
                code = payload.get("code")
                if not isinstance(code, str) or not re.fullmatch(r"[a-f0-9]{32}", code):
                    raise ValidationError("Invalid demo code.", code="demo.not_found")
                return await manager.disable(actor, code)
            case _:
                raise ValidationError("Unknown demo operation.", code="demo.invalid_input")

    return app
