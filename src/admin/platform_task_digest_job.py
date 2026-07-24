"""Watcher digest for platform tasks — a 20-minute SLIDING WINDOW per task.

The manager queues an event (platform_task_event) on every status change
and every new comment; this SYNC job (scheduler rule) turns those into one
batched email per watcher.

The window, precisely: the FIRST un-notified event of a task anchors a
20-minute window (window closes at that event's created_at + 20 min).
Events arriving during the window join it WITHOUT moving the anchor — so an
active discussion still notifies on time, it does not push the send forever.
When the window has closed, one digest per watcher goes out and every event
in the window is stamped notified_at (the idempotence marker: a processed
event never re-enters a digest, even if the job runs twice).

Positions held:
- THE ACTOR IS NEVER NOTIFIED OF THEIR OWN ACTION: each watcher's digest
  drops the events they authored; a watcher who authored everything in the
  window gets no mail.
- A watcher without a usable email is skipped silently (no error).
- Demo recipients are covered by the send_email sink (is_demo_recipient),
  the single send path — no second channel here.
- FR-only content: the admin surface is not translated.
"""

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.agent import Agent
from shared.models.platform_task import PlatformTask
from shared.models.platform_task_event import PlatformTaskEvent
from shared.models.platform_task_watcher import PlatformTaskWatcher
from src.core.config import get_settings
from src.core.email import send_email
from src.core.email_templates import task_watcher_digest_email

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]

WINDOW_MINUTES = 20


def send_platform_task_watcher_digests(
    db: Session, *, log: LogFn, dry_run: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    window = timedelta(minutes=WINDOW_MINUTES)
    tasks_url = f"{get_settings().frontend_url}/admin/tasks"

    events = (
        db.execute(
            select(PlatformTaskEvent)
            .where(
                PlatformTaskEvent.notified_at.is_(None),
                PlatformTaskEvent.created_at <= now,
            )
            .order_by(PlatformTaskEvent.created_at)
        )
        .scalars()
        .all()
    )
    by_task: dict[Any, list[PlatformTaskEvent]] = defaultdict(list)
    for event in events:
        by_task[event.task_id].append(event)

    stats = {"tasks": 0, "mails": 0, "dry_run": dry_run}
    for task_id, task_events in by_task.items():
        # The window is anchored on the FIRST un-notified event; a later
        # event never pushes it back (task_events is created_at-ordered).
        if now < task_events[0].created_at + window:
            continue  # window still open — wait
        stats["tasks"] += 1
        sent = _send_task_digest(db, task_id, task_events, tasks_url, dry_run=dry_run)
        stats["mails"] += sent
        if not dry_run:
            # Stamp EVERY event of the closed window, whoever it reached —
            # the idempotence marker. An event authored by the only watcher
            # produced no mail but is still processed (never re-queued).
            for event in task_events:
                event.notified_at = now
            db.commit()

    log(f"task watcher digest: {stats['mails']} mail(s) over {stats['tasks']} task(s)")
    return stats


def _send_task_digest(
    db: Session,
    task_id: Any,
    events: list[PlatformTaskEvent],
    tasks_url: str,
    *,
    dry_run: bool,
) -> int:
    task = db.get(PlatformTask, task_id)
    if task is None:  # deleted mid-window (CASCADE would have removed events)
        return 0
    watchers = db.execute(
        select(Agent.id, Agent.email)
        .join(PlatformTaskWatcher, PlatformTaskWatcher.agent_id == Agent.id)
        .where(PlatformTaskWatcher.task_id == task_id)
    ).all()
    sent = 0
    for agent_id, email in watchers:
        if not email:
            continue  # no usable email → skip silently (requirement 7)
        # The actor is never notified of their OWN action: drop this
        # watcher's own events. If nothing is left, no mail for them.
        visible = [e for e in events if e.actor_agent_id != agent_id]
        if not visible:
            continue
        status_changes = [
            (e.old_status or "", e.new_status or "")
            for e in visible
            if e.event_type == "status_change"
        ]
        comments = [
            (e.actor_name or "Un opérateur", e.excerpt or "")
            for e in visible
            if e.event_type == "comment"
        ]
        if not status_changes and not comments:
            continue  # never an empty digest
        if dry_run:
            sent += 1
            continue
        content = task_watcher_digest_email(task.title, status_changes, comments, tasks_url)
        try:
            send_email(email, content.subject, content.text, content.html)
        except Exception:  # noqa: BLE001 — best-effort boundary, never blocks
            logger.warning("platform task digest mail failed to=%s", email, exc_info=True)
            continue
        sent += 1
    return sent
