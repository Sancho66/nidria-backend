"""Read-only probe (sonde) of the TO_APPROVE backlog, per agency.

Answers the constat of the 13/08 lot: how many reminders wait for an
approval that never comes, since when, and how many of those are the
AUTOMATIC follow-ups (the feature sold as « les relances partent sans
qu'on y pense »). House pattern (v0.110.1): zero writes, run on the Fly
machine — `python scripts/prod_pending_reminders_probe.py`.
"""

import asyncio
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from shared.models.agency import Agency  # noqa: E402
from shared.models.client_case import ClientCase  # noqa: E402
from shared.models.reminder import Reminder  # noqa: E402
from src.core.database import async_session_maker  # noqa: E402


async def main() -> int:
    now = datetime.now(UTC)
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(
                    Agency.slug,
                    Agency.name,
                    Agency.settings,
                    Reminder.status,
                    Reminder.auto_threshold_days,
                    Reminder.created_at,
                    Reminder.scheduled_at,
                )
                .join(ClientCase, ClientCase.agency_id == Agency.id)
                .join(Reminder, Reminder.case_id == ClientCase.id)
            )
        ).all()

    per_agency: dict[str, dict] = defaultdict(
        lambda: {
            "name": "",
            "auto_enabled": True,
            "to_approve": 0,
            "to_approve_auto": 0,
            "approved": 0,
            "sent": 0,
            "cancelled": 0,
            "oldest": None,
            "newest": None,
            "oldest_scheduled": None,
        }
    )
    for slug, name, settings, status, threshold, created_at, scheduled_at in rows:
        bucket = per_agency[slug]
        bucket["name"] = name
        bucket["auto_enabled"] = (settings or {}).get("auto_reminders_enabled", True)
        if status in bucket:
            bucket[status] += 1
        if status != "to_approve":
            continue
        if threshold is not None:
            bucket["to_approve_auto"] += 1
        if bucket["oldest"] is None or created_at < bucket["oldest"]:
            bucket["oldest"] = created_at
        if bucket["newest"] is None or created_at > bucket["newest"]:
            bucket["newest"] = created_at
        if bucket["oldest_scheduled"] is None or scheduled_at < bucket["oldest_scheduled"]:
            bucket["oldest_scheduled"] = scheduled_at

    print(f"{'agency':<28} {'auto':<5} {'wait':>5} {'auto':>5} {'sent':>5} {'age j':>6}  oldest")
    print("-" * 92)
    total_waiting = 0
    for slug, b in sorted(per_agency.items(), key=lambda kv: -kv[1]["to_approve"]):
        if b["to_approve"] == 0 and b["sent"] == 0:
            continue
        total_waiting += b["to_approve"]
        age = f"{(now - b['oldest']).days}" if b["oldest"] else "-"
        oldest = b["oldest"].date().isoformat() if b["oldest"] else "-"
        print(
            f"{slug[:28]:<28} {str(b['auto_enabled']):<5} {b['to_approve']:>5} "
            f"{b['to_approve_auto']:>5} {b['sent']:>5} {age:>6}  {oldest}"
        )
    print("-" * 92)
    print(f"TOTAL en attente d'approbation : {total_waiting}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
