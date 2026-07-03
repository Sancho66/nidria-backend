"""Rebuild the usage milestones of ONE agency (or all) from real data +
the event layer — the corrective path when the incremental aggregate is
suspected wrong. Deterministic and idempotent.

Run:  uv run python scripts/replay_usage.py [agency-slug|all]
"""

import asyncio
import sys

from sqlalchemy import select

from shared.models.agency import Agency
from src.core.database import async_session_maker
from src.usage.usage_backfill import replay_usage_milestones


async def main(target: str) -> int:
    async with async_session_maker() as db:
        if target == "all":
            agencies = list((await db.execute(select(Agency))).scalars())
        else:
            agency = (
                await db.execute(select(Agency).where(Agency.slug == target))
            ).scalar_one_or_none()
            if agency is None:
                print(f"NOT FOUND: no agency with slug {target!r}")
                return 1
            agencies = [agency]
        for agency in agencies:
            milestones = await replay_usage_milestones(db, agency.id)
            print(f"{agency.slug}: {len(milestones)} milestones rebuilt")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: replay_usage.py <agency-slug|all>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
