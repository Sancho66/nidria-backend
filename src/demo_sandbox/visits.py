"""Record only anonymous demo openings, outside the read-only business session."""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from src.demo_sandbox.schema import DemoTourEvent


async def record_visit(
    db: AsyncSession,
    code: str,
    visit_id: uuid.UUID,
    tour: DemoTourEvent | None = None,
    *,
    trial_clicked: bool = False,
    reason: str | None = None,
) -> None:
    agency = await db.scalar(
        select(Agency).where(Agency.slug == f"demo-sandbox-{code}").with_for_update()
    )
    if agency is None or agency.settings.get("sandbox_enabled", True) is not True:
        return
    instant = datetime.now(UTC)
    now = instant.isoformat()
    full = agency.settings.get("sandbox_full_demo", {})
    if full and datetime.fromisoformat(full["expires_at"]) <= datetime.now(UTC):
        return
    previous = agency.settings.get("sandbox_visits", {})
    # No IP, cookie, user agent or real identity. A tab retains this random
    # nonce across refreshes and face changes. Bound the deduplication memory.
    key = hashlib.sha256(visit_id.bytes).hexdigest()
    recent = list(previous.get("recent", []))
    fresh = key not in recent
    from src.core.config import get_settings

    if get_settings().demo_measurement_enabled:
        from src.demo_sandbox.measurement import record_measurement

        measured_fresh, reason = await record_measurement(
            db, code, key, instant, reason, tour, trial_clicked
        )
        fresh = fresh and measured_fresh
    if reason is not None:
        await db.commit()
        return
    if fresh:
        recent = [*recent[-511:], key]
    agency.settings = {
        **agency.settings,
        "sandbox_visits": {
            "first": previous.get("first", now),
            "last": now,
            "count": previous.get("count", 0) + int(fresh),
            "recent": recent,
        },
    }
    if tour is not None and full:
        previous_tour = agency.settings.get("sandbox_tour", {})
        completed = previous_tour.get("completed_at")
        agency.settings = {
            **agency.settings,
            "sandbox_tour": {
                "started_at": previous_tour.get("started_at") or (now if tour.step > 0 else None),
                "completed_at": completed
                or (now if tour.status == "completed" and tour.step == 8 else None),
                "last_step": max(previous_tour.get("last_step", 0), tour.step),
                "status": "completed"
                if completed or (tour.status == "completed" and tour.step == 8)
                else tour.status
                if tour.status != "completed"
                else "started",
            },
        }
    await db.commit()


async def track_visit(
    code: str,
    visit_id: uuid.UUID,
    tour: DemoTourEvent | None = None,
    *,
    trial_clicked: bool = False,
    reason: str | None = None,
) -> None:
    # The HTTP reader keeps SELECT-only credentials. The only public writer
    # entry is this fixed operation, after the code's scope was resolved.
    from src.demo_sandbox.control import control_db

    async for db in control_db():
        await record_visit(db, code, visit_id, tour, trial_clicked=trial_clicked, reason=reason)
