from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency


class DemoSignupRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def attributed(self, identifiers: list[str]) -> list[tuple[str, datetime]]:
        if not identifiers:
            return []
        return [
            (content, created_at)
            for content, created_at in (
                await self.db.execute(
                    select(Agency.utm_content, Agency.created_at).where(
                        Agency.utm_source == "demo", Agency.utm_content.in_(identifiers)
                    )
                )
            ).all()
        ]

    async def unattributed(self) -> int:
        # A source-only signup cannot be assigned to a lot by guesswork.
        return int(
            await self.db.scalar(
                select(func.count())
                .select_from(Agency)
                .where(
                    Agency.utm_source == "demo",
                    ~func.coalesce(Agency.utm_content, "").op("~")(r"^demo:[a-f0-9]{64}$"),
                )
            )
            or 0
        )
