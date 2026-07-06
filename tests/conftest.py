# Deterministic settings for the test run, set BEFORE any `src` import
# (src.core.database calls get_settings() at import time). Real env vars
# take precedence over `.env` in pydantic-settings, so tests never depend
# on the developer's local `.env`. DATABASE_URL stays a placeholder: every
# test session goes through the testcontainer engine via the get_db
# override, never through the module-level engine.
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://placeholder/placeholder")
os.environ.setdefault("DATABASE_URL_SYNC", "postgresql+psycopg2://placeholder/placeholder")
os.environ.setdefault("JWT_AGENT_SECRET", "test-agent-secret")
os.environ.setdefault("JWT_EXPAT_SECRET", "test-expat-secret")
os.environ.setdefault("JWT_REFRESH_SECRET", "test-refresh-secret")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("ENVIRONMENT", "test")
# Force-mock every external service (plain assignment, not setdefault: a
# developer's env may flip a service to real for a live smoke test, and
# the suite must never silently hit real APIs).
os.environ["MOCK_SERVICES"] = "true"
os.environ["MOCK_EMAIL"] = "true"
# AI translation: a blank key means the REAL client refuses to call out
# (ai.not_configured) — no test can ever hit Z.ai; list prices pinned so
# the points math never depends on a developer's .env.
os.environ["AI_TRANSLATION_API_KEY"] = ""
os.environ["AI_TRANSLATION_PRICE_INPUT_USD_PER_MTOK"] = "0.1"
os.environ["AI_TRANSLATION_PRICE_OUTPUT_USD_PER_MTOK"] = "0.4"

from collections.abc import AsyncGenerator, Generator  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from shared.models import Base  # noqa: E402
from src.core.database import get_db  # noqa: E402
from src.core.dependencies import get_sync_session_local  # noqa: E402
from src.main import app  # noqa: E402

pytest_plugins = [
    "tests.plugins.agency_plugin",
    "tests.plugins.agent_plugin",
    "tests.plugins.case_plugin",
    "tests.plugins.expat_plugin",
    "tests.plugins.journey_plugin",
    "tests.plugins.rbac_plugin",
    "tests.plugins.reminder_plugin",
]


@pytest.fixture(autouse=True)
def clear_mock_sinks() -> Generator[None, None, None]:
    from src.core import email, storage

    email.outbox.clear()
    storage.mock_store.clear()
    yield
    email.outbox.clear()
    storage.mock_store.clear()


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest_asyncio.fixture(scope="session")
async def async_engine(
    postgres_container: PostgresContainer,
) -> AsyncGenerator[AsyncEngine, None]:
    sync_url = postgres_container.get_connection_url()
    # Testcontainers returns a postgresql+psycopg2:// URL; switch to asyncpg.
    async_url = sync_url.replace("+psycopg2", "+asyncpg")
    if "+asyncpg" not in async_url:
        async_url = async_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # NullPool: asyncpg connections are bound to the event loop they were
    # created on; pooling across function-scoped test loops would hand out
    # dead connections.
    engine = create_async_engine(async_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Mirror production's RLS posture in the test DB: RLS ENABLED with
        # NO policy (deny-all) on every table — exactly what the
        # a103249eb0a1_enable_rls_all_public_tables migration sets in prod.
        # The testcontainer role owns the tables (and is a superuser), so it
        # is EXEMPT from RLS (no FORCE) — the whole suite runs unchanged,
        # proving the app is intact under RLS. (test_rls.py adds the
        # deny-all proof for a non-bypass role.)
        for table in Base.metadata.sorted_tables:
            await conn.execute(text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY'))
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(async_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_maker = async_sessionmaker(bind=async_engine, expire_on_commit=False)
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.close()
            # Clean state for the next test (managers call commit() so a
            # simple rollback would not undo the writes) — Q6 decision:
            # truncate in teardown, Prism approach.
            async with async_engine.begin() as conn:
                for table in reversed(Base.metadata.sorted_tables):
                    await conn.execute(table.delete())


@pytest.fixture(scope="session")
def sync_engine(
    postgres_container: PostgresContainer, async_engine: AsyncEngine
) -> Generator[Engine, None, None]:
    """Sync engine on the SAME testcontainer DB (schema created by
    async_engine) — for the scheduler-side code (rule: API async,
    scheduler sync)."""
    sync_url = postgres_container.get_connection_url()
    if "+psycopg2" not in sync_url:
        sync_url = sync_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(sync_url, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def sync_session_local(sync_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=sync_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, sync_session_local: sessionmaker[Session]
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client against the REAL app, with get_db overridden to the
    testcontainer session (shared by enforce via the dependency cache)
    and the sync session factory pointed at the same DB (job trigger)."""

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_sync_session_local] = lambda: sync_session_local
    # Background workers (AI translation) open their OWN session — point
    # their factory at the SAME testcontainer engine.
    from src.journeys import translation_manager

    translation_manager.session_factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    translation_manager.session_factory = None
    app.dependency_overrides.clear()
