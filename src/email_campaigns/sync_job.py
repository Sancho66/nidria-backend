"""The platform job `email_sync`: Nidria's email state kept in line with
Lemlist, on the existing job machinery (`job_config`, `job_run`, the
scheduler, the `/jobs` API).

Ported from Prism's `sync_job.py` (reference commit 629a2b0), rewritten for
Nidria's single platform job (rule N8): no project, no legacy pass, no
`EMAIL_SYNC_*` switches (the cron and the activation live in `job_config`,
seeded DISABLED by `DEFAULT_JOB_CONFIGS`).

`run_email_sync(db, log=..., dry_run=...) -> dict`, the contract of
`src/core/job_wrapper.run_job`. One run:
1. real effects off (or Lemlist mocked, or no key), or a manual run asking
   `dry_run`: `{"skipped": true, "reason": "dry_run"}` and NO call at all
   (decision A6: `run_job` records a SUCCESS with these stats, it knows no
   "skipped" decided by a pipeline). The env's master switch off:
   `{"skipped": true, "reason": "module_disabled"}`, no call either;
2. the team the platform key opens (`GET /team`, cached ten minutes); when
   Lemlist does not say, no agency is read and the error is counted;
3. the agencies whose module is on AND bound to that team (rule N4: at most
   one today; any other agency switched on is skipped and counted);
4. per agency, a session-level advisory lock
   `pg_try_advisory_lock(hashtextextended('email_sync:' || agency_id, 0))`
   on its own connection (held: the agency is "busy", skipped and counted;
   released at the end), then each ACTIVE or PAUSED campaign at Lemlist
   reconciled in its own transaction (`EmailSyncManager.reconcile_campaign`):
   one failing agency or campaign fails alone and keeps its checkpoint, the
   others go on, `errors` says it.

Toward Lemlist the job only READS: nothing is pushed, nobody is launched,
nothing is paused there, no AI value is generated. The async part runs in
`asyncio.run(...)` on the run's OWN async engine in `NullPool` (the run is
in a worker thread, on its own event loop, which the API's pooled engine
must never be used from).

Statistics (Prism's, without `legacy`): `campaigns`, `campaigns_failed`,
`contacts`, `new_events`, `duplicate_events`, `sent`, `opened`, `clicked`,
`replies`, `bounces`, `unsubscribed`, `reply_contents`, `missing_leads`,
`pause_changes`, `dry_run_campaigns`, `errors`, `error_messages` (codes with
the agency and campaign they are about, never sentences, at most ten), plus
`agencies` (read) and `agencies_skipped` (switched on but not bound to the
key's team, or busy), and `variable_unsubscribed` (a contact's variable
unsubscribed at Lemlist: stored, never applied). Log lines carry ids,
counts and codes only.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from shared.models.agency_email_module import AgencyEmailModule
from src.core.config import Settings, get_settings
from src.email_campaigns.email_manager import EmailManager
from src.email_campaigns.email_sync import AgencyReconcile, EmailSyncManager
from src.email_campaigns.flags import external_effects_on
from src.email_campaigns.lemlist_team import TeamUnknownError, team_of_key

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]

JOB_ID = "email_sync"
JOB_NAME = "Email Sync (Lemlist)"
#: What a skipped run says, and why.
SKIPPED_DRY_RUN = "dry_run"
SKIPPED_MODULE_DISABLED = "module_disabled"
#: The advisory lock's key, per agency (spec 10.3).
LOCK_SQL = text("SELECT pg_try_advisory_lock(hashtextextended('email_sync:' || :agency, 0))")
UNLOCK_SQL = text("SELECT pg_advisory_unlock(hashtextextended('email_sync:' || :agency, 0))")
#: Error entries kept in the stats, at most.
MAX_ERROR_MESSAGES = 10

STAT_KEYS = (
    "campaigns",
    "campaigns_failed",
    "contacts",
    "new_events",
    "duplicate_events",
    "sent",
    "opened",
    "clicked",
    "replies",
    "bounces",
    "unsubscribed",
    "variable_unsubscribed",
    "reply_contents",
    "missing_leads",
    "pause_changes",
    "dry_run_campaigns",
    "errors",
    "agencies",
    "agencies_skipped",
)


def make_async_engine(settings: Settings) -> AsyncEngine:
    """The run's own engine: the job runs in a worker thread, on its own
    event loop, which the API's pooled engine must never be used from."""
    return create_async_engine(settings.database_url, poolclass=NullPool)


def _engine_of(db: Session) -> Engine:
    bind = db.get_bind()
    return bind if isinstance(bind, Engine) else bind.engine


@contextmanager
def agency_lock(db: Session, agency_id: uuid.UUID) -> Iterator[bool]:
    """A session-level advisory lock on a dedicated connection (the job's own
    session commits every log line and may change connection in between):
    True when this run holds the agency, False when another run does."""
    with _engine_of(db).connect() as connection:
        params = {"agency": str(agency_id)}
        acquired = bool(connection.scalar(LOCK_SQL, params))
        connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    connection.execute(UNLOCK_SQL, params)
                    connection.commit()
                except Exception:
                    # Never hand a possibly locked physical connection back.
                    connection.invalidate()
                    raise


async def _reconcile(settings: Settings, agency_id: uuid.UUID, log: LogFn) -> AgencyReconcile:
    engine = make_async_engine(settings)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            sync = EmailSyncManager(session, EmailManager(session, settings=settings))
            return await sync.reconcile_agency(agency_id, log)
    finally:
        await engine.dispose()


def _empty_stats() -> dict[str, Any]:
    stats: dict[str, Any] = dict.fromkeys(STAT_KEYS, 0)
    stats["error_messages"] = []
    return stats


def _error(stats: dict[str, Any], code: str, **about: Any) -> None:
    stats["errors"] += 1
    if len(stats["error_messages"]) < MAX_ERROR_MESSAGES:
        stats["error_messages"].append(
            {"code": code, **{key: str(value) for key, value in about.items()}}
        )


def _add(stats: dict[str, Any], report: AgencyReconcile) -> None:
    """One agency's reconciliation into the run's statistics."""
    by_type: Counter[str] = Counter()
    for campaign in report.campaigns:
        by_type.update(campaign.by_type)
        stats["campaigns"] += 1
        stats["campaigns_failed"] += int(campaign.failed)
        stats["contacts"] += campaign.contacts
        stats["new_events"] += campaign.events_new
        stats["duplicate_events"] += campaign.events_duplicate
        stats["reply_contents"] += campaign.reply_contents
        stats["missing_leads"] += campaign.leads_missing
        stats["pause_changes"] += campaign.pause_changes
        stats["unsubscribed"] += campaign.unsubscribed
        stats["variable_unsubscribed"] += campaign.variable_unsubscribed
        for code in campaign.errors:
            _error(stats, code, agency_id=report.agency_id, campaign_id=campaign.campaign_id)
    stats["sent"] += by_type["SENT"]
    stats["opened"] += by_type["OPENED"]
    stats["clicked"] += by_type["CLICKED"]
    stats["replies"] += by_type["REPLIED"]
    stats["bounces"] += by_type["BOUNCED"]
    stats["unsubscribed"] += by_type["UNSUBSCRIBED"]
    stats["dry_run_campaigns"] += report.dry_run_campaigns
    if report.error is not None:
        _error(stats, report.error, agency_id=report.agency_id)


def run_email_sync(db: Session, log: LogFn | None = None, dry_run: bool = False) -> dict[str, Any]:
    """One run of the platform job (see the module docstring)."""
    log_fn: LogFn = log or (lambda _message: None)
    settings = get_settings()
    if dry_run or not external_effects_on(settings):
        log_fn("email_sync: skipped (dry_run): no Lemlist call")
        return {"skipped": True, "reason": SKIPPED_DRY_RUN}
    if not settings.email_module_enabled:
        log_fn("email_sync: skipped (module_disabled): EMAIL_MODULE_ENABLED is off")
        return {"skipped": True, "reason": SKIPPED_MODULE_DISABLED}

    stats = _empty_stats()
    modules = [
        (row.agency_id, row.provider_team_id)
        for row in db.execute(
            select(AgencyEmailModule)
            .where(AgencyEmailModule.enabled.is_(True))
            .order_by(AgencyEmailModule.agency_id)
        ).scalars()
    ]
    db.rollback()
    try:
        team = team_of_key(settings)
    except TeamUnknownError as exc:
        # Lemlist did not say which team the key opens: no agency is read
        # (a bound agency is never read in another team's name, A4).
        stats["agencies_skipped"] = len(modules)
        _error(stats, "email.provider_unavailable", reason=f"team_unknown:{exc.reason}")
        log_fn(f"email_sync: no agency read (team_unknown, {exc.reason})")
        return stats
    assert team is not None  # real effects on: a team, or TeamUnknownError

    for agency_id, team_id in modules:
        if team_id != team.id:
            stats["agencies_skipped"] += 1
            log_fn(f"agency {agency_id}: skipped (not bound to the key's Lemlist team)")
            continue
        with agency_lock(db, agency_id) as acquired:
            if not acquired:
                stats["agencies_skipped"] += 1
                log_fn(f"agency {agency_id}: skipped (busy: another run holds it)")
                continue
            log_fn(f"agency {agency_id}: reading Lemlist")
            try:
                report = asyncio.run(_reconcile(settings, agency_id, log_fn))
            except Exception:  # noqa: BLE001 (one agency never stops the others)
                logger.exception("email sync of agency %s failed", agency_id)
                _error(stats, "internal_error", agency_id=agency_id)
                log_fn(f"agency {agency_id}: failed (internal_error)")
                continue
        stats["agencies"] += 1
        _add(stats, report)
    log_fn(
        f"email_sync: {stats['agencies']} agencies, {stats['campaigns']} campaigns, "
        f"{stats['new_events']} new events, {stats['errors']} errors"
    )
    return stats


__all__ = ["JOB_ID", "JOB_NAME", "agency_lock", "make_async_engine", "run_email_sync"]
