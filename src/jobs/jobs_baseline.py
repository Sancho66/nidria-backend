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
    {
        "job_id": "platform_task_watcher_digest",
        "name": "Platform task watcher digest (20-minute sliding window)",
        # Every minute: the window is 20 min, this only decides how promptly
        # a closed window is flushed (at most ~1 min late).
        "cron_expression": "* * * * *",
    },
    {
        "job_id": "onboarding_email",
        "name": "Onboarding email (~10 min after an agency is created)",
        # Every 5 minutes: the delay is 10 min, this only decides how
        # precisely the mail lands on it (between J+10 and J+15).
        "cron_expression": "*/5 * * * *",
    },
    {
        "job_id": "activation_reminders",
        "name": "Activation reminders (J+3 / J+7 on an unactivated client space)",
        # Quotidien : le calendrier est en JOURS, un balayage par jour suffit
        # (et une invitation ratée d'un tick rattrape au suivant).
        "cron_expression": "0 8 * * *",
    },
    {
        "job_id": "sweep_document_template_drafts",
        "name": "Sweep abandoned document-template drafts (>24 h)",
        # Horaire : le TTL est en HEURES, un balayage par heure ne fait que
        # borner le retard (au plus ~1 h après le terme) — et un brouillon
        # abandonné ne coûte rien tant qu'il dort, il n'est vu de personne.
        "cron_expression": "15 * * * *",
    },
    {
        "job_id": "expire_agent_invitations",
        "name": "Expire agent invitations (return their seats)",
        # Hourly: invitations live 7 days — the sweep only bounds how late
        # a dead invitation's seat comes back (at most ~1 h, well within
        # the next-cycle décrue it feeds).
        "cron_expression": "30 * * * *",
    },
]


async def seed_job_configs(db: AsyncSession) -> None:
    existing = {config.job_id for config in (await db.execute(select(JobConfig))).scalars()}
    for spec in DEFAULT_JOB_CONFIGS:
        if spec["job_id"] not in existing:
            db.add(JobConfig(**spec))
    await db.commit()
