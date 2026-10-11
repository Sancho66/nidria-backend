"""Scheduler control battery: superadmin-only access (job.manage is a
platform permission since 08/10/2026), cron in data, hot-reload,
pause→SKIPPED, manual trigger through the same wrapper, dry_run without
mutation, disabled job not scheduled at boot, seed never overwrites."""

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
from src.core.rbac.baseline import PLATFORM_PERMISSIONS
from src.core.rbac.permissions import Permission
from src.core.scheduler import JOB_REGISTRY, build_scheduler
from src.jobs.jobs_baseline import seed_job_configs
from src.jobs.jobs_router import BINDINGS as JOBS_BINDINGS
from src.main import app
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.reminder_plugin import MakeReminder

_NOW = datetime.now(UTC)

# One request per /jobs route — (method, bound template, url, body) — that
# the superadmin's call passes; the run id is unknown on purpose (its 404
# comes from the handler, i.e. past the gate). Must cover jobs_router's
# BINDINGS exactly (asserted below), so a new route cannot skip the barrier.
JOB_ROUTES: list[tuple[str, str, str, dict[str, object] | None]] = [
    ("GET", "/jobs", "/jobs", None),
    (
        "PATCH",
        "/jobs/{job_id}",
        "/jobs/dispatch_reminders",
        {"cron_expression": "0 12 * * *"},
    ),
    (
        "POST",
        "/jobs/{job_id}/pause",
        "/jobs/dispatch_reminders/pause",
        {"until": (_NOW + timedelta(hours=2)).isoformat()},
    ),
    ("POST", "/jobs/{job_id}/resume", "/jobs/dispatch_reminders/resume", None),
    ("POST", "/jobs/{job_id}/trigger", "/jobs/dispatch_reminders/trigger", {"dry_run": True}),
    ("GET", "/jobs/{job_id}/runs", "/jobs/dispatch_reminders/runs", None),
    (
        "GET",
        "/jobs/{job_id}/runs/{run_id}",
        "/jobs/dispatch_reminders/runs/00000000-0000-0000-0000-000000000000",
        None,
    ),
]
_ROUTE_IDS = [f"{method} {template}" for method, template, _, _ in JOB_ROUTES]


@pytest_asyncio.fixture
async def jobs_client(
    client: AsyncClient, rbac_baseline: None, db_session: AsyncSession
) -> AsyncClient:
    await seed_job_configs(db_session)
    return client


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    # The scheduler's only operator: an agency admin no longer holds job.manage.
    return await make_agent(role=system_roles["superadmin"])


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
    superadmin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(role=system_roles["member"])
    assert (await jobs_client.get("/jobs", headers=agent_headers(member))).status_code == 403
    agency_admin = await make_agent(role=system_roles["admin"])
    assert (await jobs_client.get("/jobs", headers=agent_headers(agency_admin))).status_code == 403
    response = await jobs_client.get("/jobs", headers=agent_headers(superadmin))
    assert response.status_code == 200
    assert {j["job_id"] for j in response.json()} == {
        "dispatch_reminders",
        "auto_reminders",
        "trial_nurture",
        "notification_digest",
        "platform_task_watcher_digest",
        "expire_agent_invitations",
        "onboarding_email",
        "activation_reminders",
        "sweep_document_template_drafts",
        "email_sync",
        "email_auto_import",
    }


def test_every_jobs_route_is_gated_by_the_platform_permission() -> None:
    assert {(method, template) for method, template, _, _ in JOB_ROUTES} == {
        (binding.method, binding.path) for binding in JOBS_BINDINGS
    }
    assert {binding.permission for binding in JOBS_BINDINGS} == {Permission.JOB_MANAGE}
    assert Permission.JOB_MANAGE in PLATFORM_PERMISSIONS


@pytest.mark.parametrize(("method", "template", "url", "body"), JOB_ROUTES, ids=_ROUTE_IDS)
async def test_an_agency_admin_is_refused_every_jobs_route(
    jobs_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    method: str,
    template: str,
    url: str,
    body: dict[str, object] | None,
) -> None:
    """A job serves every agency: an agency admin can no longer enable,
    trigger, pause or read it. The matrix refuses before any handler."""
    agency_admin = await make_agent(role=system_roles["admin"])
    response = await jobs_client.request(
        method, url, headers=agent_headers(agency_admin), json=body
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "permission.denied"
    assert response.json()["params"] == {"permission": "job.manage"}


@pytest.mark.parametrize(("method", "template", "url", "body"), JOB_ROUTES, ids=_ROUTE_IDS)
async def test_the_superadmin_operates_every_jobs_route(
    jobs_client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    method: str,
    template: str,
    url: str,
    body: dict[str, object] | None,
) -> None:
    response = await jobs_client.request(method, url, headers=agent_headers(superadmin), json=body)
    if template.endswith("{run_id}"):
        # Past the gate: the unknown run is the handler's own 404.
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Job run not found."
    else:
        assert response.status_code == 200, response.text


# --- hot reload --------------------------------------------------------------------


async def test_patch_cron_reschedules_running_scheduler(
    jobs_client: AsyncClient,
    superadmin: Agent,
    live_scheduler: BackgroundScheduler,
    agent_headers: AuthHeaders,
) -> None:
    assert live_scheduler.get_job("dispatch_reminders") is not None
    response = await jobs_client.patch(
        "/jobs/dispatch_reminders",
        headers=agent_headers(superadmin),
        json={"cron_expression": "0 12 * * *"},
    )
    assert response.status_code == 200
    assert response.json()["cron_expression"] == "0 12 * * *"
    job = live_scheduler.get_job("dispatch_reminders")
    assert job is not None
    assert "hour='12'" in str(job.trigger)

    invalid = await jobs_client.patch(
        "/jobs/dispatch_reminders",
        headers=agent_headers(superadmin),
        json={"cron_expression": "not a cron"},
    )
    assert invalid.status_code == 422


async def test_patch_disable_unschedules(
    jobs_client: AsyncClient,
    superadmin: Agent,
    live_scheduler: BackgroundScheduler,
    agent_headers: AuthHeaders,
) -> None:
    response = await jobs_client.patch(
        "/jobs/auto_reminders", headers=agent_headers(superadmin), json={"is_enabled": False}
    )
    assert response.status_code == 200
    assert live_scheduler.get_job("auto_reminders") is None


async def test_disabled_job_not_scheduled_at_boot(
    jobs_client: AsyncClient,
    superadmin: Agent,
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
    superadmin: Agent,
    sync_session_local: sessionmaker[Session],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(superadmin)
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
    superadmin: Agent,
    db_session: AsyncSession,
    due_approved_reminder: tuple[ClientCase, object],
    agent_headers: AuthHeaders,
) -> None:
    response = await jobs_client.post(
        "/jobs/dispatch_reminders/trigger",
        headers=agent_headers(superadmin),
        json={"dry_run": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["triggered_by"] == "manual"
    assert body["triggered_by_agent_id"] == str(superadmin.id)
    assert body["stats"] == {"due": 1, "sent": 1, "emails": 1, "skipped_step_done": 0}
    assert len(email.outbox) == 1

    run_row = (await db_session.execute(select(JobRun))).scalar_one()
    assert run_row.triggered_by == "manual"


async def test_trigger_dry_run_mutates_nothing(
    jobs_client: AsyncClient,
    superadmin: Agent,
    db_session: AsyncSession,
    due_approved_reminder: tuple[ClientCase, object],
    agent_headers: AuthHeaders,
) -> None:
    _, reminder = due_approved_reminder
    response = await jobs_client.post(
        "/jobs/dispatch_reminders/trigger",
        headers=agent_headers(superadmin),
        json={"dry_run": True},
    )
    assert response.status_code == 200
    assert response.json()["stats"] == {"due": 1, "sent": 0, "dry_run": True}
    await db_session.refresh(reminder)
    assert reminder.status == "approved"  # type: ignore[attr-defined]
    assert email.outbox == []


async def test_run_listing_and_detail_with_log(
    jobs_client: AsyncClient,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(superadmin)
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
    # dispatch, auto, nurture, digest, platform_task_watcher, expire_invitations,
    # onboarding, activation_reminders, sweep_document_template_drafts, email_sync,
    # email_auto_import
    assert total == 11
