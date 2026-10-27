import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.demo_transport import DemoTransport
from src.core.exceptions import ValidationError
from src.demo_reporting.repository import DemoSignupRepository
from src.demo_reporting.schema import DemoStatsQuery, DemoStatsResponse, DemoStatsSnapshot
from src.demo_sandbox.measurement import PARIS

REPORT_ACTOR = uuid.uuid5(uuid.NAMESPACE_URL, "nidria:read-only-demo-report")


class DemoReportingManager:
    def __init__(self, db: AsyncSession) -> None:
        self.repo = DemoSignupRepository(db)

    async def read(self, lot: str, day: date | None) -> DemoStatsResponse:
        today = datetime.now(PARIS).date()
        query = DemoStatsQuery(lot=lot, date=day or today - timedelta(days=1))
        if query.date > today:
            raise ValidationError(
                "A report date cannot be in the future.", code="demo.invalid_input"
            )
        source = DemoStatsSnapshot.model_validate(
            await DemoTransport().read_stats(REPORT_ACTOR, query.model_dump(mode="json"))
        )
        signups = await self.repo.attributed([demo.demo_id for demo in source.demos])
        linked = {identifier for identifier, _ in signups}
        for demo in source.demos:
            demo.signup = demo.demo_id in linked
        source.totals.signups = len(signups)
        source.daily.totals.signups = sum(
            source.daily.start_utc <= created_at < source.daily.end_utc for _, created_at in signups
        )
        source.coverage.unattributed_demo_signups_all_batches = await self.repo.unattributed()
        source.generated_at = datetime.now(UTC)
        source.demos = [demo for demo in source.demos if demo.visits > 0]
        return DemoStatsResponse.model_validate(source.model_dump())
