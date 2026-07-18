"""Scheduler control battery: cron in data, hot-reload, pause→SKIPPED,
manual trigger through the same wrapper, dry_run without mutation,
disabled job not scheduled at boot, seed never overwrites."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.job import JobConfig, JobRun
from shared.models.rbac import Role
from src.core import email
from src.core.enums import JobTriggeredBy
from src.core.job_wrapper import run_job
from src.core.scheduler import JOB_REGISTRY, build_scheduler
from src.jobs.jobs_baseline import seed_job_configs
from src.main import app
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.reminder_plugin import MakeReminder

_NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def jobs_client(
    client: AsyncClient, rbac_baseline: None, db_session: AsyncSession
) -> AsyncClient:
    await seed_job_configs(db_session)
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest.fixture
def live_scheduler(
    sync_session_local: sessionmaker[Session],
) -> Generator[BackgroundScheduler, None, None]:
    """A real (paused) scheduler wired on app.state for hot-reload tests."""
    scheduler = build_scheduler(sync_session_local)
    scheduler.start(paused=True)
    app.state.scheduler = scheduler
    app.state.sync_session_local = sync_session_local
    yield scheduler
    scheduler.shutdown(wait=False)
    del app.state.scheduler
    del app.state.sync_session_local


def _run(session_local: sessionmaker[Session], job_id: str) -> JobRun:
    with session_local() as db:
        return run_job(
            db,
            job_id=job_id,
            pipeline=JOB_REGISTRY[job_id],
            triggered_by=JobTriggeredBy.SCHEDULER,
        )


# --- access ---------------------------------------------------------------------


async def test_jobs_require_job_manage(
    jobs_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(role=system_roles["member"])
    assert (await jobs_client.get("/jobs", headers=agent_headers(member))).status_code == 403
    response = await jobs_client.get("/jobs", headers=agent_headers(admin))
    assert response.status_code == 200
    assert {j["job_id"] for j in response.json()} == {
        "dispatch_reminders",
        "auto_reminders",
        "trial_nurture",
        "notification_digest",
    }


# --- hot reload --------------------------------------------------------------------


async def test_patch_cron_reschedules_running_scheduler(
    jobs_client: AsyncClient,
    admin: Agent,
    live_scheduler: BackgroundScheduler,
    agent_headers: AuthHeaders,
) -> None:
    assert live_scheduler.get_job("dispatch_reminders") is not None
    response = await jobs_client.patch(
        "/jobs/dispatch_reminders",
        headers=agent_headers(admin),
        json={"cron_expression": "0 12 * * *"},
    )
    assert response.status_code == 200
    assert response.json()["cron_expression"] == "0 12 * * *"
    job = live_scheduler.get_job("dispatch_reminders")
    assert job is not None
    assert "hour='12'" in str(job.trigger)

    invalid = await jobs_client.patch(
        "/jobs/dispatch_reminders",
        headers=agent_headers(admin),
        json={"cron_expression": "not a cron"},
    )
    assert invalid.status_code == 422


async def test_patch_disable_unschedules(
    jobs_client: AsyncClient,
    admin: Agent,
    live_scheduler: BackgroundScheduler,
    agent_headers: AuthHeaders,
) -> None:
    response = await jobs_client.patch(
        "/jobs/auto_reminders", headers=agent_headers(admin), json={"is_enabled": False}
    )
    assert response.status_code == 200
    assert live_scheduler.get_job("auto_reminders") is None


async def test_disabled_job_not_scheduled_at_boot(
    jobs_client: AsyncClient,
    admin: Agent,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    agent_headers: AuthHeaders,
) -> None:
    config = (
        await db_session.execute(select(JobConfig).where(JobConfig.job_id == "auto_reminders"))
    ).scalar_one()
    config.is_enabled = False
    await db_session.commit()

    scheduler = build_scheduler(sync_session_local)
    scheduler.start(paused=True)
    try:
        assert scheduler.get_job("auto_reminders") is None
        assert scheduler.get_job("dispatch_reminders") is not None
    finally:
        scheduler.shutdown(wait=False)


# --- pause / resume -------------------------------------------------------------------


async def test_pause_makes_tick_skip(
    jobs_client: AsyncClient,
    admin: Agent,
    sync_session_local: sessionmaker[Session],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    paused = await jobs_client.post(
        "/jobs/dispatch_reminders/pause",
        headers=headers,
        json={"until": (_NOW + timedelta(hours=2)).isoformat()},
    )
    assert paused.status_code == 200

    run = _run(sync_session_local, "dispatch_reminders")
    assert run.status == "skipped"
    assert "paused" in run.log_output

    resumed = await jobs_client.post("/jobs/dispatch_reminders/resume", headers=headers)
    assert resumed.status_code == 200
    run_after = _run(sync_session_local, "dispatch_reminders")
    assert run_after.status == "success"


# --- manual trigger ---------------------------------------------------------------------


@pytest_asyncio.fixture
async def due_approved_reminder(
    rbac_baseline: None, make_client_case: MakeClientCase, make_reminder: MakeReminder
) -> tuple[ClientCase, object]:
    case = await make_client_case()
    reminder = await make_reminder(
        case=case, status="approved", scheduled_at=_NOW - timedelta(hours=1)
    )
    return case, reminder


async def test_trigger_manual_creates_run_and_dispatches(
    jobs_client: AsyncClient,
    admin: Agent,
    db_session: AsyncSession,
    due_approved_reminder: tuple[ClientCase, object],
    agent_headers: AuthHeaders,
) -> None:
    response = await jobs_client.post(
        "/jobs/dispatch_reminders/trigger",
        headers=agent_headers(admin),
        json={"dry_run": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["triggered_by"] == "manual"
    assert body["triggered_by_agent_id"] == str(admin.id)
    assert body["stats"] == {"due": 1, "sent": 1}
    assert len(email.outbox) == 1

    run_row = (await db_session.execute(select(JobRun))).scalar_one()
    assert run_row.triggered_by == "manual"


async def test_trigger_dry_run_mutates_nothing(
    jobs_client: AsyncClient,
    admin: Agent,
    db_session: AsyncSession,
    due_approved_reminder: tuple[ClientCase, object],
    agent_headers: AuthHeaders,
) -> None:
    _, reminder = due_approved_reminder
    response = await jobs_client.post(
        "/jobs/dispatch_reminders/trigger",
        headers=agent_headers(admin),
        json={"dry_run": True},
    )
    assert response.status_code == 200
    assert response.json()["stats"] == {"due": 1, "sent": 0, "dry_run": True}
    await db_session.refresh(reminder)
    assert reminder.status == "approved"  # type: ignore[attr-defined]
    assert email.outbox == []


async def test_run_listing_and_detail_with_log(
    jobs_client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await jobs_client.post("/jobs/auto_reminders/trigger", headers=headers, json={"dry_run": True})
    runs = await jobs_client.get("/jobs/auto_reminders/runs", headers=headers)
    assert runs.status_code == 200
    assert len(runs.json()) == 1
    run_id = runs.json()[0]["id"]
    detail = await jobs_client.get(f"/jobs/auto_reminders/runs/{run_id}", headers=headers)
    assert detail.status_code == 200
    assert isinstance(detail.json()["log_output"], str)


# --- seed -----------------------------------------------------------------------------------


async def test_seed_never_overwrites_runtime_edits(db_session: AsyncSession) -> None:
    await seed_job_configs(db_session)
    config = (
        await db_session.execute(select(JobConfig).where(JobConfig.job_id == "dispatch_reminders"))
    ).scalar_one()
    config.cron_expression = "5 5 * * *"
    await db_session.commit()

    await seed_job_configs(db_session)
    await db_session.refresh(config)
    assert config.cron_expression == "5 5 * * *"
    total = len((await db_session.execute(select(JobConfig))).scalars().all())
    assert total == 4  # dispatch, auto, nurture, digest
