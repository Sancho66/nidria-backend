"""The platform job `email_auto_import` (Alexandre's decision of 11/10/2026:
« import auto via un script cron »): the campaigns created on Lemlist's side
come into Nidria by themselves, by the import's own rules
(`EmailImportManager.auto_import`, `email_import`'s module docstring, step
3), on the existing job machinery (`job_config`, `job_run`, the scheduler,
the `/jobs` API). Seeded DISABLED (`DEFAULT_JOB_CONFIGS`): switched on at
go-live, like `email_sync`.

`run_email_auto_import(db, log=..., dry_run=...) -> dict`, the contract of
`src/core/job_wrapper.run_job`. One run:
1. real effects off (or Lemlist mocked, or no key), or a manual run asking
   `dry_run`: `{"skipped": true, "reason": "dry_run"}` and no call; the
   env's master switch off: `{"skipped": true, "reason":
   "module_disabled"}`, no call either;
2. the team the platform key opens (`GET /team`, cached ten minutes); when
   Lemlist does not say, no agency is read and the error is counted;
3. the agencies whose module is on AND bound to that team (rule N4: at most
   one today; any other agency switched on is skipped and counted);
4. per agency, `email_sync`'s own session-level advisory lock
   (`sync_job.agency_lock`, held: the agency is "busy", skipped and
   counted, the next run comes back), so the two jobs never read Lemlist
   for one agency at once; then `auto_import` on the run's own async engine
   in NullPool (the run is in a worker thread, on its own event loop, like
   `email_sync`'s): the team's campaigns listed (one call) and, only when
   something is due, the import itself, one campaign per transaction under
   the agency's import lock (a manual import waits for it, and it for one).
   One agency failing never stops the others.

Statistics: `agencies` (read), `agencies_skipped` (switched on but not bound
to the key's team, or busy with `email_sync`), `campaigns_listed`,
`campaigns_new` (unknown to Nidria, imported now), `campaigns_stale`
(imported, with leads Nidria does not know, imported again),
`campaigns_imported` (answered ok), `campaigns_failed`,
`members_created`, `members_updated`, `profiles_created`, `conflicts`,
`unknown_leads` (left after the import), `calls` (Lemlist), `errors` and
`error_messages` (codes with the agency and the Lemlist campaign they are
about, never sentences, at most ten). Log lines carry ids, counts and codes
only: never an address nor a person's name.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from shared.models.agency_email_module import AgencyEmailModule
from src.core.config import Settings, get_settings
from src.core.exceptions import NidriaError
from src.email_campaigns import sync_job
from src.email_campaigns.email_import import AutoImport, EmailImportManager
from src.email_campaigns.email_manager import EmailManager
from src.email_campaigns.flags import external_effects_on
from src.email_campaigns.lemlist_team import TeamUnknownError, team_of_key

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]

JOB_ID = "email_auto_import"
JOB_NAME = "Email auto-import (Lemlist)"
#: What a skipped run says, and why (the same words as `email_sync`).
SKIPPED_DRY_RUN = sync_job.SKIPPED_DRY_RUN
SKIPPED_MODULE_DISABLED = sync_job.SKIPPED_MODULE_DISABLED
#: Error entries kept in the stats, at most.
MAX_ERROR_MESSAGES = 10

STAT_KEYS = (
    "agencies",
    "agencies_skipped",
    "campaigns_listed",
    "campaigns_new",
    "campaigns_stale",
    "campaigns_imported",
    "campaigns_failed",
    "members_created",
    "members_updated",
    "profiles_created",
    "conflicts",
    "unknown_leads",
    "calls",
    "errors",
)


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


def _add(stats: dict[str, Any], agency_id: uuid.UUID, outcome: AutoImport) -> None:
    """One agency's automatic import into the run's statistics."""
    stats["campaigns_listed"] += outcome.listed
    stats["campaigns_new"] += outcome.new
    stats["campaigns_stale"] += outcome.stale
    stats["calls"] += outcome.calls
    if outcome.result is None:
        return
    for row in outcome.result.campaigns:
        if not row.ok:
            stats["campaigns_failed"] += 1
            _error(
                stats,
                row.code or "internal_error",
                agency_id=agency_id,
                provider_campaign_id=row.provider_campaign_id,
            )
            continue
        stats["campaigns_imported"] += 1
        stats["members_created"] += row.members_created
        stats["members_updated"] += row.members_updated
        stats["profiles_created"] += row.profiles_created
        stats["conflicts"] += row.conflicts
        stats["unknown_leads"] += row.unknown_leads or 0


def describe(agency_id: uuid.UUID, outcome: AutoImport) -> str:
    """One log line for an agency: ids, counts and codes only."""
    line = (
        f"agency {agency_id}: {outcome.listed} Lemlist campaigns, {outcome.new} new, "
        f"{outcome.stale} with unknown leads"
    )
    if outcome.result is None:
        return line + ", nothing to import"
    done = [row for row in outcome.result.campaigns if row.ok]
    failed = [row for row in outcome.result.campaigns if not row.ok]
    line += (
        f", {len(done)} imported ({sum(r.members_created for r in done)} memberships and "
        f"{sum(r.profiles_created for r in done)} client records created, "
        f"{sum(r.conflicts for r in done)} conflicts set aside), {outcome.calls} Lemlist calls"
    )
    if failed:
        line += ", failed: " + ", ".join(
            f"{row.provider_campaign_id} ({row.code})" for row in failed
        )
    return line


async def _auto_import(settings: Settings, agency_id: uuid.UUID) -> AutoImport:
    # The run's own engine (`sync_job.make_async_engine`, which tests point
    # at their database), never the API's pooled one.
    engine = sync_job.make_async_engine(settings)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            manager = EmailImportManager(session, EmailManager(session, settings=settings))
            return await manager.auto_import(agency_id)
    finally:
        await engine.dispose()


def run_email_auto_import(
    db: Session, log: LogFn | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """One run of the platform job (see the module docstring)."""
    log_fn: LogFn = log or (lambda _message: None)
    settings = get_settings()
    if dry_run or not external_effects_on(settings):
        log_fn("email_auto_import: skipped (dry_run): no Lemlist call")
        return {"skipped": True, "reason": SKIPPED_DRY_RUN}
    if not settings.email_module_enabled:
        log_fn("email_auto_import: skipped (module_disabled): EMAIL_MODULE_ENABLED is off")
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
        log_fn(f"email_auto_import: no agency read (team_unknown, {exc.reason})")
        return stats
    assert team is not None  # real effects on: a team, or TeamUnknownError

    for agency_id, team_id in modules:
        if team_id != team.id:
            stats["agencies_skipped"] += 1
            log_fn(f"agency {agency_id}: skipped (not bound to the key's Lemlist team)")
            continue
        with sync_job.agency_lock(db, agency_id) as acquired:
            if not acquired:
                stats["agencies_skipped"] += 1
                log_fn(f"agency {agency_id}: skipped (busy: the Email Sync job reads it)")
                continue
            log_fn(f"agency {agency_id}: listing the team's Lemlist campaigns")
            try:
                outcome = asyncio.run(_auto_import(settings, agency_id))
            except NidriaError as exc:
                # The module switched off, Lemlist unavailable or refusing:
                # the route's own refusal, by its code.
                _error(stats, exc.code, agency_id=agency_id)
                log_fn(f"agency {agency_id}: not imported ({exc.code})")
                continue
            except Exception:  # noqa: BLE001 (one agency never stops the others)
                logger.exception("email auto-import of agency %s failed", agency_id)
                _error(stats, "internal_error", agency_id=agency_id)
                log_fn(f"agency {agency_id}: failed (internal_error)")
                continue
        stats["agencies"] += 1
        _add(stats, agency_id, outcome)
        log_fn(describe(agency_id, outcome))
    log_fn(
        f"email_auto_import: {stats['agencies']} agencies, {stats['campaigns_new']} new "
        f"campaigns, {stats['campaigns_imported']} imported, {stats['errors']} errors"
    )
    return stats


__all__ = ["JOB_ID", "JOB_NAME", "run_email_auto_import"]
