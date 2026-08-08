"""Résorbe les sièges lecteur vacants — le cas transitoire de la
simplification radicale (08/08).

Before the new model, a reader pool could exceed the active readers (a
« siège payé non attribué » lingering after a deactivation). The new model
makes deactivation drop the pool automatically, so that state can no longer
be CREATED — but rows born under the old model may still carry pool >
active readers. This one-shot resorbs them: pool := active readers, a
`reader_seats.released` usage event, and the Paddle quantity pushed DOWN
(full_next_billing_period — the started month stays due, the decrease lands
next cycle), exactly like a member deactivation.

DRY-RUN BY DEFAULT (house convention): --execute to write. Reads DB +
talks to Paddle, so it runs on the Fly machine (the dev IP is Cloudflare-
blocked): `python scripts/resorb_reader_vacancies.py [--execute]`.

Constat 08/08: ZERO agencies concerned in prod (the front had not wired
the reader purchase yet) — this is defensive, a no-op there.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from shared.models.agency import Agency  # noqa: E402
from src.agencies.agencies_manager import AgenciesManager  # noqa: E402
from src.billing.billing_manager import BillingManager  # noqa: E402
from src.core.database import async_session_maker  # noqa: E402
from src.core.enums import ActorType  # noqa: E402
from src.usage.usage_manager import UsageManager  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Write (default: dry-run).")
    args = parser.parse_args()

    async with async_session_maker() as db:
        agencies = (
            (await db.execute(select(Agency).where(Agency.reader_seats_purchased > 0)))
            .scalars()
            .all()
        )
        manager = AgenciesManager(db)
        concerned = []
        for agency in agencies:
            # COMMITTED readers (règle 08/08: inviter = payer) — a pending
            # reader invitation OWNS its auto-bought seat: resorbing it
            # would free a seat the invitee is walking toward.
            active = await manager.committed_reader_count(agency.id)
            if agency.reader_seats_purchased > active:
                concerned.append((agency, active))

        print(f"{len(agencies)} agencies with a reader pool; {len(concerned)} with vacants.")
        for agency, active in concerned:
            surplus = agency.reader_seats_purchased - active
            print(f"  {agency.slug}: pool {agency.reader_seats_purchased} → {active} (−{surplus})")

        if not concerned:
            print("Nothing to resorb.")
            return 0
        if not args.execute:
            print("DRY-RUN — pass --execute to resorb.")
            return 0

        for agency, active in concerned:
            agency.reader_seats_purchased = active
            await UsageManager(db).emit(
                agency_id=agency.id,
                event_type="reader_seats.released",
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                details={"pool": active, "reason": "vacancy_resorb"},
            )
            await db.commit()
            # Push the new pool DOWN (best-effort, like a deactivation).
            try:
                await BillingManager(db).sync_seat_quantity(agency.id, increase=False)
            except Exception as exc:  # noqa: BLE001 — one hiccup must not abort the sweep
                print(f"  !! {agency.slug}: Paddle push failed ({exc}) — pool set, re-run to push")
        print(f"Resorbed {len(concerned)} agencies.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
