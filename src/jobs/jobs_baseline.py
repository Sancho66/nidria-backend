"""Default job configs — seeded by the test harness, the dev snippet
and scripts/seed.py (step 14). Create-if-absent, NEVER overwrite a
runtime edit (same rule as the system roles)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.job import JobConfig

DEFAULT_JOB_CONFIGS: list[dict[str, str]] = [
    {
        "job_id": "dispatch_reminders",
        "name": "Dispatch approved reminders",
        "cron_expression": "* * * * *",
    },
    {
        "job_id": "auto_reminders",
        "name": "Create J+20/J+30 follow-up reminders",
        "cron_expression": "0 7 * * *",
    },
    {
        "job_id": "trial_nurture",
        "name": "Trial nurture emails (J+7 / J+21 / J+28)",
        "cron_expression": "0 8 * * *",
    },
    {
        "job_id": "notification_digest",
        "name": "Progress digest (weekly on Monday / daily per agency pref)",
        "cron_expression": "0 9 * * *",
    },
]


async def seed_job_configs(db: AsyncSession) -> None:
    existing = {config.job_id for config in (await db.execute(select(JobConfig))).scalars()}
    for spec in DEFAULT_JOB_CONFIGS:
        if spec["job_id"] not in existing:
            db.add(JobConfig(**spec))
    await db.commit()
