"""Aggregate the demo catalogue in the sandbox's read-only transaction."""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Integer, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.demo_telemetry import visits
from src.admin.demo_schema import (
    DemoActivityCounts,
    DemoActivityDay,
    DemoAdminStats,
    DemoAdminStatsQuery,
    DemoBatchActivity,
)
from src.demo_sandbox.catalog import DemoCatalog
from src.demo_sandbox.lifecycle import expiry_expression, search_conditions
from src.demo_sandbox.measurement import paris_window
from src.demo_sandbox.stats import DemoStatsRepository


class DemoAdminAnalyticsRepository:
    def __init__(self, db: AsyncSession, catalog: DemoCatalog) -> None:
        self.db = db
        self.catalog = catalog

    async def aggregate(
        self, query: DemoAdminStatsQuery, now: datetime, since: datetime
    ) -> dict[str, Any]:
        conditions = search_conditions(query, self.catalog)
        count = cast(func.coalesce(Agency.settings["sandbox_visits"]["count"].astext, "0"), Integer)
        first = cast(Agency.settings["sandbox_visits"]["first"].astext, DateTime(timezone=True))
        batch = func.coalesce(Agency.settings["sandbox_batch"].astext, "")
        expiry = expiry_expression()
        live = or_(expiry.is_(None), expiry > now)
        enabled = func.coalesce(Agency.settings["sandbox_enabled"].astext, "true") == "true"
        metrics = [
            func.count().label("total"),
            func.count().filter(count > 0).label("opened"),
            func.coalesce(func.sum(count), 0).label("visits"),
            func.count()
            .filter(Agency.settings["sandbox_tour"]["started_at"].astext.is_not(None))
            .label("tour_started"),
            func.count()
            .filter(Agency.settings["sandbox_tour"]["completed_at"].astext.is_not(None))
            .label("tour_completed"),
        ]
        totals = (
            (
                await self.db.execute(
                    select(
                        *metrics,
                        func.count().filter(and_(enabled, live)).label("active"),
                        func.count().filter(and_(~enabled, live)).label("disabled"),
                        func.count().filter(expiry <= now).label("expired"),
                        func.count().filter(first < since).label("historical"),
                    )
                    .select_from(Agency)
                    .where(*conditions)
                )
            )
            .mappings()
            .one()
        )
        batches = (
            (
                await self.db.execute(
                    select(batch.label("batch"), *metrics)
                    .select_from(Agency)
                    .where(*conditions)
                    .group_by(batch)
                    .order_by(func.coalesce(func.sum(count), 0).desc(), batch)
                )
            )
            .mappings()
            .all()
        )
        today = now.astimezone(ZoneInfo("Europe/Paris")).date()
        first_day = today - timedelta(days=query.days - 1)
        population = select(Agency.slug).where(*conditions).cte("population")
        accepted = visits.join(population, population.c.slug == "demo-sandbox-" + visits.c.code)
        event_conditions = [
            visits.c.day >= first_day,
            visits.c.day <= today,
            visits.c.excluded_reason.is_(None),
        ]
        daily = (
            (
                await self.db.execute(
                    select(
                        visits.c.day,
                        func.count().label("visits"),
                        func.count(func.distinct(visits.c.code)).label("opened"),
                    )
                    .select_from(accepted)
                    .where(*event_conditions)
                    .group_by(visits.c.day)
                )
            )
            .mappings()
            .all()
        )
        period = (
            (
                await self.db.execute(
                    select(
                        func.count().label("visits"),
                        func.count(func.distinct(visits.c.code)).label("opened"),
                        func.count(func.distinct(visits.c.code))
                        .filter(visits.c.trial_clicked_at.is_not(None))
                        .label("trial_clicked"),
                    )
                    .select_from(accepted)
                    .where(*event_conditions)
                )
            )
            .mappings()
            .one()
        )
        return {
            "totals": dict(totals),
            "batches": batches,
            "daily": daily,
            "period": period,
            "first_day": first_day,
        }


class DemoAdminAnalyticsManager:
    def __init__(self, db: AsyncSession, catalog: DemoCatalog) -> None:
        self.repo = DemoAdminAnalyticsRepository(db, catalog)
        self.coverage = DemoStatsRepository(db)

    async def read(self, query: DemoAdminStatsQuery) -> DemoAdminStats:
        now = datetime.now(UTC)
        since = await self.coverage.started_at()
        source = await self.repo.aggregate(query, now, since)
        facts = {row["day"]: row for row in source["daily"]}
        days = []
        for offset in range(query.days):
            day = source["first_day"] + timedelta(days=offset)
            start, end = paris_window(day)
            fact = facts.get(day, {})
            days.append(
                DemoActivityDay(
                    date=day,
                    visits=fact.get("visits", 0),
                    opened=fact.get("opened", 0),
                    complete=start >= since and end <= now,
                )
            )
        totals = source["totals"]
        return DemoAdminStats(
            generated_at=now,
            days=query.days,
            totals=DemoActivityCounts.model_validate(totals),
            active=totals["active"],
            disabled=totals["disabled"],
            expired=totals["expired"],
            period_visits=source["period"]["visits"],
            period_opened=source["period"]["opened"],
            period_trial_clicked=source["period"]["trial_clicked"],
            detailed_since=since,
            historical_unqualified=totals["historical"] > 0,
            daily=days,
            batches=[DemoBatchActivity.model_validate(row) for row in source["batches"]],
        )
