"""Anonymous sessions and opening days, never identified returning people."""

from typing import Any

from sqlalchemy import Integer, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.demo_telemetry import visits
from src.core.config import get_settings


def tour_step() -> Any:
    return cast(func.coalesce(Agency.settings["sandbox_tour"]["last_step"].astext, "0"), Integer)


def tour_status() -> Any:
    return func.coalesce(Agency.settings["sandbox_tour"]["status"].astext, "not_started")


def measured_count(*, days: bool = False) -> Any:
    if not get_settings().demo_measurement_enabled:
        return literal(0)
    return (
        select(func.count(func.distinct(visits.c.day if days else visits.c.visit_hash)))
        .where(
            "demo-sandbox-" + visits.c.code == Agency.slug,
            visits.c.excluded_reason.is_(None),
        )
        .correlate(Agency)
        .scalar_subquery()
    )


def rollup() -> Any:
    return (
        select(
            visits.c.code,
            func.count(func.distinct(visits.c.visit_hash)).label("sessions"),
            func.count(func.distinct(visits.c.day)).label("days"),
        )
        .where(visits.c.excluded_reason.is_(None))
        .group_by(visits.c.code)
        .subquery()
    )


class DemoEngagementRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def for_codes(self, codes: list[str]) -> dict[str, dict[str, int]]:
        if not codes or not get_settings().demo_measurement_enabled:
            return {}
        source = rollup()
        rows = (await self.db.execute(select(source).where(source.c.code.in_(codes)))).all()
        observed = {
            row.code: {"measured_sessions": row.sessions, "active_days": row.days} for row in rows
        }
        return {
            code: observed.get(code, {"measured_sessions": 0, "active_days": 0}) for code in codes
        }
