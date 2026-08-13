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
# Ryuk, the testcontainers reaper: a SECOND container per xdist worker whose
# only job is to kill ours if the process dies outright. We do not need it —
# `postgres_container` is a `with` block, so the container is stopped on
# normal exit AND on any exception; Ryuk only covers a hard kill (SIGKILL, a
# CI timeout). The price of that cover is 8 extra container starts per run
# plus a REAL race, reproduced here on an idle box: the library calls
# `.waiting_for(...)` AFTER `.start()`, so it never waits for Ryuk to be
# ready before reading its mapped port, and the run dies with
#   ConnectionError: Port mapping for container <id> and port 8080 is not available
# (measured: 6 of 8 workers lost, 1281 errors, on a machine with no other
# suite running — a failure we had been blaming on session collisions).
# Ported from prism-backend/tests/conftest.py, which fixed the same thing.
# If a run IS killed hard, clean up BY LABEL, never by image — the local dev
# database is the same postgres:16-alpine:
#   docker ps -aq --filter label=org.testcontainers=true | xargs docker rm -f
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
os.environ.setdefault("ENVIRONMENT", "test")
# Password hashing: the MEASURED number-one cost of this suite. At the
# production factor (12) the run spent 829s across workers in bcrypt —
# 2697 hashes at ~296ms, 24.6% of all test time — purely to prove that
# `make_agent()` can write a password nobody attacks. Factor 4 makes the
# same call ~1ms. NOTHING is weakened: the hashing tests still hash, still
# verify, still reject a wrong password and a garbage digest, and
# test_security.py additionally proves a factor-12 digest (what production
# rows carry) still verifies while the suite runs at 4.
# The safety net lives in Settings._refuse_weak_bcrypt: a factor below 12
# outside ENVIRONMENT=test REFUSES to boot, so this speed-up cannot leak
# into a real deployment. `BCRYPT_ROUNDS=12 uv run pytest` opts back in.
os.environ.setdefault("BCRYPT_ROUNDS", "4")
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
# Nurture: the booking URL now lives in developers' .env (prod
# activation 2026-07-07) — pin it EMPTY so the J+28 held/pending_config
# semantics stay testable whatever the local .env carries.
os.environ["NURTURE_BOOKING_URL"] = ""

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
    "tests.plugins.signature_plugin",
]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-tag the slow alembic-roundtrip tests (every tests/*migration*.py,
    ~12-19s each) with the `migration` marker — like `seed`, they only matter
    when you touch migrations. `make check` skips them (-m "not seed and not
    migration") for a fast local loop; `make test-migrations` runs them, and
    CI runs the WHOLE suite (marker filter is local-only). New *migration* files
    are covered automatically (no per-file edit)."""
    for item in items:
        if "migration" in item.path.name:
            item.add_marker(pytest.mark.migration)


@pytest.fixture(autouse=True)
def clear_mock_sinks() -> Generator[None, None, None]:
    from src.core import email, ratelimit, storage

    email.outbox.clear()
    storage.mock_store.clear()
    # Le limiteur in-process (signup + webhook DocuSeal) : fenêtre vierge
    # entre tests — un worker xdist enchaîne des dizaines d'appels webhook
    # dans la même minute, le seuil prod (120/min) deviendrait flaky ici.
    ratelimit.reset()
    yield
    email.outbox.clear()
    storage.mock_store.clear()
    ratelimit.reset()


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    # TEST-ONLY durability OFF. A test DB is thrown away on every run, so
    # fsync / WAL sync / full-page-writes buy nothing but disk I/O — a
    # dominant per-test cost (commit + TRUNCATE). With fsync=off +
    # synchronous_commit=off the writes stay in the OS page cache (RAM) and
    # are never forced to disk, so an explicit tmpfs adds nothing (and the
    # postgres image declares its data dir a VOLUME, which shadows a tmpfs
    # mount anyway). SAME Postgres, SAME engine, SAME SQL/DDL/JSONB/enums:
    # ZERO semantic change, only durability a test never needs is removed —
    # proven ~37% on isolated I/O (commit+truncate); the whole-suite wall gain
    # is real but not cleanly measurable on a loaded box (run-to-run variance
    # ~135s); 1292 passed IDENTICAL, no flaky. NEVER apply to a real database.
    # PYTEST_PG_DURABLE=1 opts out (A/B / paranoia; the app has no durability
    # edge that needs it).
    pg = PostgresContainer("postgres:16-alpine")
    if not os.environ.get("PYTEST_PG_DURABLE"):
        pg = pg.with_command(
            "postgres -c fsync=off -c synchronous_commit=off "
            "-c full_page_writes=off -c autovacuum=off"
        )
    with pg as container:
        yield container


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
            # truncate in teardown, Prism approach. ONE TRUNCATE of every
            # table (vs a per-table DELETE loop: 62 round-trips/test) —
            # identical isolation (all tables emptied), one statement.
            # No RESTART IDENTITY (matches DELETE: sequences untouched);
            # CASCADE is a no-op safety net since ALL tables are listed.
            async with async_engine.begin() as conn:
                tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
                await conn.execute(text(f"TRUNCATE {tables} CASCADE"))


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
