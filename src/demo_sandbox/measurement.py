"""Anonymous daily facts, separate from fictional customer records."""

import hashlib
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.demo_telemetry import measurement, metadata, visits
from src.demo_sandbox.schema import DemoTourEvent

PARIS = ZoneInfo("Europe/Paris")
OBVIOUS_BOT = re.compile(
    r"bot\b|crawler|spider|headlesschrome|preview|slack|discord|telegram|"
    r"facebookexternalhit|whatsapp|proofpoint|mimecast|barracuda|urlscan|safe.?links",
    re.IGNORECASE,
)


def demo_attribution(code: str) -> str:
    return "demo:" + hashlib.sha256(("nidria-demo:" + code).encode()).hexdigest()


def paris_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, PARIS)
    end = datetime.combine(day + timedelta(days=1), time.min, PARIS)
    return start.astimezone(UTC), end.astimezone(UTC)


def excluded_reason(admin_preview: bool, automated: bool, user_agent: str) -> str | None:
    if admin_preview:
        return "admin_preview"
    if automated or OBVIOUS_BOT.search(user_agent):
        return "obvious_robot"
    return None


async def prepare_measurement(db: AsyncSession) -> None:
    """Explicit seed/test operation; never called by a public GET."""
    connection = await db.connection()
    await connection.run_sync(metadata.create_all)
    for table in ("demo_measurement", "demo_telemetry_visit"):
        await db.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    await db.execute(insert(measurement).values(key="daily-v1").on_conflict_do_nothing())


async def record_measurement(
    db: AsyncSession,
    code: str,
    key: str,
    now: datetime,
    reason: str | None,
    tour: DemoTourEvent | None,
    trial_clicked: bool,
) -> tuple[bool, str | None]:
    """Called under the agency row lock; return first visit and its classification."""
    existing = (
        await db.execute(
            select(visits.c.excluded_reason)
            .where(visits.c.code == code, visits.c.visit_hash == key)
            .order_by(visits.c.first_at)
            .limit(1)
        )
    ).first()
    reason = existing.excluded_reason if existing is not None else reason
    values = dict(
        code=code,
        visit_hash=key,
        day=now.astimezone(PARIS).date(),
        first_at=now,
        last_at=now,
        excluded_reason=reason,
        tour_started_at=now if tour and tour.step == 1 and tour.status == "started" else None,
        tour_completed_at=now if tour and tour.status == "completed" else None,
        trial_clicked_at=now if trial_clicked else None,
        last_step=tour.step if tour else 0,
    )
    from sqlalchemy import func

    statement = insert(visits).values(**values)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[visits.c.code, visits.c.visit_hash, visits.c.day],
            set_={
                "last_at": now,
                "tour_started_at": func.coalesce(
                    visits.c.tour_started_at, values["tour_started_at"]
                ),
                "tour_completed_at": func.coalesce(
                    visits.c.tour_completed_at, values["tour_completed_at"]
                ),
                "trial_clicked_at": func.coalesce(
                    visits.c.trial_clicked_at, values["trial_clicked_at"]
                ),
                "last_step": func.greatest(visits.c.last_step, values["last_step"]),
            },
        )
    )
    return existing is None, reason
