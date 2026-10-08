"""The scheduler brought in line with `job_config` every minute (decision of
08/10/2026): a job switched on, off or re-timed by another process (the
administration script) or by another machine's PATCH takes effect in the
running API without a restart.

`reconcile_jobs` reads only; a job enabled and known is programmed with its
trigger, again when the trigger changed (cron, timezone); a disabled one is
removed; an unchanged one keeps its programming, its next run untouched; an
unknown job_id is ignored, the reconciliation's own id included; one bad row
fails alone. The reconciliation is the scheduler's own interval job, never a
`run_job` (no `job_run`), and APScheduler's two lines per run of it are
dropped from the log.
"""

import logging
from collections.abc import Generator
from typing import Any

import pytest
import pytest_asyncio
from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.job import JobConfig, JobRun
from src.core.scheduler import (
    JOB_REGISTRY,
    RECONCILE_INTERVAL_SECONDS,
    RECONCILE_JOB_ID,
    build_scheduler,
    reconcile_jobs,
)
from src.jobs.jobs_baseline import DEFAULT_JOB_CONFIGS, seed_job_configs

#: Seeded: every default job enabled but email_sync.
ENABLED = len(DEFAULT_JOB_CONFIGS) - 1
NOTHING = {"scheduled": 0, "rescheduled": 0, "removed": 0, "unknown": 0, "failed": 0}


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession) -> None:
    await seed_job_configs(db_session)


@pytest.fixture
def scheduler(
    seeded: None, sync_session_local: sessionmaker[Session]
) -> Generator[BackgroundScheduler, None, None]:
    """The API's scheduler as it boots, paused: nothing runs, and every next
    run is computed."""
    running = build_scheduler(sync_session_local)
    running.start(paused=True)
    yield running
    running.shutdown(wait=False)


async def set_config(db: AsyncSession, job_id: str, **values: Any) -> None:
    """A change of `job_config` the scheduler did not see (another process)."""
    await db.execute(update(JobConfig).where(JobConfig.job_id == job_id).values(**values))
    await db.commit()


def trigger_of(expression: str, timezone: str = "UTC") -> str:
    return repr(CronTrigger.from_crontab(expression, timezone=timezone))


def job(scheduler: BackgroundScheduler, job_id: str) -> Job:
    found = scheduler.get_job(job_id)
    assert found is not None, job_id
    return found


async def test_a_job_enabled_in_the_database_is_programmed_with_its_trigger(
    db_session: AsyncSession,
    scheduler: BackgroundScheduler,
    sync_session_local: sessionmaker[Session],
) -> None:
    assert scheduler.get_job("email_sync") is None  # seeded disabled
    await set_config(db_session, "email_sync", is_enabled=True)
    counts = reconcile_jobs(scheduler, sync_session_local)
    assert counts == {**NOTHING, "scheduled": 1, "unchanged": ENABLED}
    programmed = job(scheduler, "email_sync")
    assert repr(programmed.trigger) == trigger_of("*/10 * * * *")
    assert programmed.next_run_time is not None


async def test_a_changed_cron_or_timezone_is_programmed_again(
    db_session: AsyncSession,
    scheduler: BackgroundScheduler,
    sync_session_local: sessionmaker[Session],
) -> None:
    before = job(scheduler, "auto_reminders").next_run_time
    await set_config(db_session, "auto_reminders", cron_expression="0 12 * * *")
    # The timezone alone: the same cron, another hour of the world.
    await set_config(db_session, "trial_nurture", timezone="America/Argentina/Buenos_Aires")
    counts = reconcile_jobs(scheduler, sync_session_local)
    assert counts == {**NOTHING, "rescheduled": 2, "unchanged": ENABLED - 1}
    assert repr(job(scheduler, "auto_reminders").trigger) == trigger_of("0 12 * * *")
    assert job(scheduler, "auto_reminders").next_run_time != before
    assert repr(job(scheduler, "trial_nurture").trigger) == trigger_of(
        "0 8 * * *", "America/Argentina/Buenos_Aires"
    )


async def test_a_disabled_job_is_removed(
    db_session: AsyncSession,
    scheduler: BackgroundScheduler,
    sync_session_local: sessionmaker[Session],
) -> None:
    assert scheduler.get_job("dispatch_reminders") is not None
    await set_config(db_session, "dispatch_reminders", is_enabled=False)
    counts = reconcile_jobs(scheduler, sync_session_local)
    assert counts == {**NOTHING, "removed": 1, "unchanged": ENABLED}
    assert scheduler.get_job("dispatch_reminders") is None


async def test_an_unchanged_job_keeps_its_programming_and_its_next_run(
    scheduler: BackgroundScheduler, sync_session_local: sessionmaker[Session]
) -> None:
    before = {
        programmed.id: (programmed, programmed.next_run_time) for programmed in scheduler.get_jobs()
    }
    for _ in range(2):
        counts = reconcile_jobs(scheduler, sync_session_local)
        # The enabled ones, and email_sync: disabled and not programmed.
        assert counts == {**NOTHING, "unchanged": ENABLED + 1}
    after = {programmed.id: programmed for programmed in scheduler.get_jobs()}
    assert set(after) == set(before)
    for job_id, (programmed, next_run) in before.items():
        # The very job: never removed and added again.
        assert after[job_id] is programmed, job_id
        assert after[job_id].next_run_time == next_run, job_id


async def test_an_unknown_job_id_is_ignored_the_reconciliation_s_own_included(
    db_session: AsyncSession,
    scheduler: BackgroundScheduler,
    sync_session_local: sessionmaker[Session],
) -> None:
    db_session.add_all(
        [
            JobConfig(job_id="no_such_pipeline", name="Ghost", cron_expression="* * * * *"),
            # A row bearing the reconciliation's id, disabled: it never
            # takes the reconciliation out.
            JobConfig(
                job_id=RECONCILE_JOB_ID,
                name="Impostor",
                cron_expression="* * * * *",
                is_enabled=False,
            ),
        ]
    )
    await db_session.commit()
    counts = reconcile_jobs(scheduler, sync_session_local)
    assert counts == {**NOTHING, "unknown": 2, "unchanged": ENABLED + 1}
    assert scheduler.get_job("no_such_pipeline") is None
    assert isinstance(job(scheduler, RECONCILE_JOB_ID).trigger, IntervalTrigger)


async def test_a_bad_row_fails_alone(
    db_session: AsyncSession,
    scheduler: BackgroundScheduler,
    sync_session_local: sessionmaker[Session],
) -> None:
    await set_config(db_session, "email_sync", is_enabled=True, cron_expression="not a cron")
    await set_config(db_session, "auto_reminders", is_enabled=False)
    counts = reconcile_jobs(scheduler, sync_session_local)
    assert counts == {**NOTHING, "removed": 1, "failed": 1, "unchanged": ENABLED - 1}
    assert scheduler.get_job("email_sync") is None
    assert scheduler.get_job("auto_reminders") is None


async def test_the_reconciliation_writes_nothing(
    db_session: AsyncSession,
    scheduler: BackgroundScheduler,
    sync_session_local: sessionmaker[Session],
) -> None:
    await set_config(db_session, "email_sync", is_enabled=True)
    await set_config(db_session, "dispatch_reminders", is_enabled=False)

    def rows() -> list[tuple[Any, ...]]:
        with sync_session_local() as db:
            return sorted(
                (
                    c.job_id,
                    c.is_enabled,
                    c.cron_expression,
                    c.timezone,
                    c.next_run_at,
                    c.last_run_at,
                    c.last_run_status,
                    c.updated_at,
                )
                for c in db.execute(select(JobConfig)).scalars()
            )

    before = rows()
    counts = reconcile_jobs(scheduler, sync_session_local)
    assert (counts["scheduled"], counts["removed"]) == (1, 1)
    assert rows() == before
    assert await db_session.scalar(select(func.count()).select_from(JobRun)) == 0


async def test_the_reconciliation_is_the_scheduler_s_own_job_never_a_job_run(
    db_session: AsyncSession, scheduler: BackgroundScheduler
) -> None:
    assert RECONCILE_JOB_ID not in JOB_REGISTRY
    internal = job(scheduler, RECONCILE_JOB_ID)
    assert isinstance(internal.trigger, IntervalTrigger)
    assert internal.trigger.interval.total_seconds() == RECONCILE_INTERVAL_SECONDS
    await set_config(db_session, "email_sync", is_enabled=True)
    internal.func()  # as the scheduler runs it, every minute
    assert scheduler.get_job("email_sync") is not None
    assert await db_session.scalar(select(func.count()).select_from(JobRun)) == 0


def test_apscheduler_s_lines_on_each_reconciliation_are_dropped(
    scheduler: BackgroundScheduler, sync_session_local: sessionmaker[Session]
) -> None:
    executor = logging.getLogger("apscheduler.executors.default")

    def kept(level: int, about: Job) -> bool:
        record = logging.LogRecord(
            executor.name, level, __file__, 1, 'Running job "%s" (at %s)', (about, "now"), None
        )
        return bool(executor.filter(record))

    internal = job(scheduler, RECONCILE_JOB_ID)
    other = job(scheduler, "dispatch_reminders")
    assert not kept(logging.INFO, internal)
    assert kept(logging.INFO, other)
    # Its warnings and errors pass.
    assert kept(logging.WARNING, internal)
    # Booting again (a test, a reload) adds no second filter.
    filters = list(executor.filters)
    build_scheduler(sync_session_local)
    assert executor.filters == filters
