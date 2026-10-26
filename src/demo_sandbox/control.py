"""Private management channel. Never accepts production authentication tokens."""

import hmac
import re
import time
import uuid
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from src.admin.demo_schema import (
    AgencyDemoBatchRequest,
    DemoControlEnvelope,
    DemoCreateRequest,
    DemoJourneyInput,
)
from src.admin.demo_transport import MAX_BODY_BYTES, signature
from src.core.config import get_settings
from src.core.exceptions import ForbiddenError, ValidationError, register_exception_handlers
from src.demo_sandbox.catalog import DemoCatalog
from src.demo_sandbox.control_manager import DemoControlManager
from src.demo_sandbox.guards import SANDBOX_DB_HOSTS, SANDBOX_DB_NAME, assert_isolated_settings


class ListInput(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=100)
    search: str = Field(default="", max_length=200)


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


async def control_db() -> AsyncIterator[AsyncSession]:
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
        or len(body) > MAX_BODY_BYTES
        or not hmac.compare_digest(
            supplied, signature(settings.demo_control_secret, timestamp, actor, body)
        )
    ):
        raise ForbiddenError("Invalid management signature.")
    return actor_id


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

    @app.post("/demos")
    async def dispatch(
        actor: Annotated[uuid.UUID, Depends(authorize)],
        db: Annotated[AsyncSession, Depends(control_db, scope="function")],
        envelope: DemoControlEnvelope,
    ) -> Any:
        manager = DemoControlManager(db, catalog)
        payload = envelope.payload
        match envelope.operation:
            case "agency_batch":
                from src.demo_sandbox.agency_control import AgencyDemoControlManager

                return await AgencyDemoControlManager(db, catalog).create_batch(
                    actor, AgencyDemoBatchRequest.model_validate(payload)
                )
            case "list":
                query = ListInput.model_validate(payload)
                return await manager.list_demos(query.page, query.page_size, query.search)
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
