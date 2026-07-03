"""Backfill the example dossier into EXISTING agencies (nurture bloc 2).

New agencies get it at creation (wizard); agencies created BEFORE this
feature don't. This script seeds it for them — idempotent through the
`agency.settings["demo_case_seeded_at"]` marker, so re-running (or
running after an agency deleted its example) never re-creates anything.

The case is owned by the agency's EARLIEST internal member (its first
admin in practice); an agency with no internal member is skipped and
reported. Run AFTER validation, one agency first:

    uv run python scripts/seed_demo_cases.py <agency-slug>   # one agency
    uv run python scripts/seed_demo_cases.py all             # every agency
"""

import asyncio
import sys

from sqlalchemy import select

from shared.models.agency import Agency
from shared.models.agent import Agent
from src.agencies.demo_case_seed import DEMO_SEED_MARKER, seed_demo_case
from src.core.database import async_session_maker


async def main(target: str) -> int:
    async with async_session_maker() as db:
        if target == "all":
            agencies = list((await db.execute(select(Agency).order_by(Agency.slug))).scalars())
        else:
            agency = (
                await db.execute(select(Agency).where(Agency.slug == target))
            ).scalar_one_or_none()
            if agency is None:
                print(f"NOT FOUND: no agency with slug {target!r}")
                return 1
            agencies = [agency]
        for agency in agencies:
            if agency.settings.get(DEMO_SEED_MARKER):
                print(f"{agency.slug}: already seeded, skipped")
                continue
            owner = (
                await db.execute(
                    select(Agent)
                    .where(Agent.agency_id == agency.id, Agent.is_external.is_(False))
                    .order_by(Agent.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if owner is None:
                print(f"{agency.slug}: SKIPPED — no internal member to own the case")
                continue
            case = await seed_demo_case(db, agency, owner)
            print(f"{agency.slug}: demo case seeded ({case.id if case else 'noop'})")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: seed_demo_cases.py <agency-slug|all>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
