"""Read-only projection of demo metadata and anonymous daily observations."""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.demo_telemetry import measurement, visits
from src.core.exceptions import UpstreamError
from src.demo_reporting.schema import (
    DemoMetrics,
    DemoOpening,
    DemoStatsCoverage,
    DemoStatsDay,
    DemoStatsEntry,
    DemoStatsQuery,
    DemoStatsSnapshot,
)
from src.demo_sandbox.measurement import demo_attribution, paris_window


class DemoStatsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def agencies(self, batch: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in (
                await self.db.execute(
                    select(
                        Agency.slug,
                        Agency.name,
                        Agency.created_at,
                        Agency.settings["sandbox_full_demo"].label("identity"),
                        Agency.settings["sandbox_visits"].label("visits"),
                        Agency.settings["sandbox_tour"].label("tour"),
                    )
                    .where(
                        Agency.slug.startswith("demo-sandbox-"),
                        Agency.settings["sandbox_batch"].astext == batch,
                    )
                    .order_by(Agency.created_at, Agency.id)
                )
            )
            .mappings()
            .all()
        ]

    async def observed(self, codes: list[str], query: DemoStatsQuery) -> list[dict[str, Any]]:
        if not codes:
            return []
        return [
            dict(row)
            for row in (
                await self.db.execute(
                    select(visits)
                    .where(visits.c.code.in_(codes), visits.c.day == query.date)
                    .order_by(visits.c.first_at)
                )
            )
            .mappings()
            .all()
        ]

    async def clicked(self, codes: list[str]) -> set[str]:
        if not codes:
            return set()
        return set(
            await self.db.scalars(
                select(visits.c.code)
                .where(
                    visits.c.code.in_(codes),
                    visits.c.excluded_reason.is_(None),
                    visits.c.trial_clicked_at.is_not(None),
                )
                .distinct()
            )
        )

    async def started_at(self) -> datetime:
        result = await self.db.scalar(
            select(measurement.c.started_at).where(measurement.c.key == "daily-v1")
        )
        if result is None:
            raise UpstreamError("Demo measurements are not initialized.", code="demo.unavailable")
        assert isinstance(result, datetime)
        return result


class DemoStatsManager:
    def __init__(self, db: AsyncSession) -> None:
        self.repo = DemoStatsRepository(db)

    async def read(self, query: DemoStatsQuery) -> DemoStatsSnapshot:
        now = datetime.now(UTC)
        start, end = paris_window(query.date)
        since = await self.repo.started_at()
        source = await self.repo.agencies(query.lot)
        codes = [row["slug"].removeprefix("demo-sandbox-") for row in source]
        facts = await self.repo.observed(codes, query)
        clicked = await self.repo.clicked(codes)
        by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in facts:
            by_code[fact["code"]].append(fact)
        entries = []
        daily = DemoMetrics()
        legacy = False
        for code, row in zip(codes, source, strict=True):
            metadata, counts, tour = row["identity"] or {}, row["visits"] or {}, row["tour"] or {}
            observations = by_code[code]
            day = [value for value in observations if value["day"] == query.date]
            accepted_day = [value for value in day if value["excluded_reason"] is None]
            entry = DemoStatsEntry(
                demo_id=demo_attribution(code),
                email=metadata.get("email"),
                company_name=row["name"],
                created_at=row["created_at"],
                first_opened_at=counts.get("first"),
                last_opened_at=counts.get("last"),
                visits=counts.get("count", 0),
                tour_status=tour.get("status", "not_started"),
                tour_last_step=tour.get("last_step", 0),
                tour_started_at=tour.get("started_at"),
                tour_completed_at=tour.get("completed_at"),
                trial_clicked=code in clicked,
                openings_on_date=[
                    DemoOpening(
                        first_opened_at=value["first_at"],
                        last_opened_at=value["last_at"],
                        probably_robot=value["excluded_reason"] == "obvious_robot",
                        excluded_reason=value["excluded_reason"],
                    )
                    for value in day
                ],
            )
            legacy = legacy or bool(entry.first_opened_at and entry.first_opened_at < since)
            entries.append(entry)
            daily.created += int(start <= entry.created_at < end)
            daily.opened += int(bool(accepted_day))
            daily.visits += len(accepted_day)
            daily.tour_started += int(
                any(value["tour_started_at"] is not None for value in accepted_day)
            )
            daily.tour_completed += int(
                any(value["tour_completed_at"] is not None for value in accepted_day)
            )
            daily.trial_clicked += int(
                any(value["trial_clicked_at"] is not None for value in accepted_day)
            )
        complete = start >= since and end <= now
        limitations = [
            "Trial clicks and per-demo signup references were not recorded before deployment."
        ]
        if legacy:
            limitations.append(
                "Historical opening totals are preserved; "
                "past admin and robot visits cannot be reclassified."
            )
        if not complete:
            limitations.append(
                "This Paris day is incomplete; "
                "measured daily events are lower bounds, not historical zeros."
            )
        return DemoStatsSnapshot(
            lot=query.lot,
            generated_at=now,
            totals=DemoMetrics(
                created=len(entries),
                opened=sum(entry.visits > 0 for entry in entries),
                visits=sum(entry.visits for entry in entries),
                tour_started=sum(entry.tour_started_at is not None for entry in entries),
                tour_completed=sum(entry.tour_completed_at is not None for entry in entries),
                trial_clicked=sum(entry.trial_clicked for entry in entries),
            ),
            daily=DemoStatsDay(
                date=query.date, start_utc=start, end_utc=end, complete=complete, totals=daily
            ),
            coverage=DemoStatsCoverage(
                detailed_since=since,
                trial_clicks_since=since,
                historical_totals_include_unqualified_visits=legacy,
                limitations=limitations,
            ),
            demos=entries,
        )
