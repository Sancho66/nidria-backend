"""Read-only probe (sonde) of the seat rule against REAL agency rows.

Post-deploy verification of the 05/08 decision, house pattern (v0.110.1):
zero writes — evaluates seats_max_for() on every agency and checks the
rule table category by category:

  no active subscription (trial, no plan, dead paddle sub) → 3
  active subscription (manual or paddle, past_due included) → None

Run on the Fly machine: `python scripts/prod_seat_rule_probe.py`.
Exit 1 on any row disagreeing with the rule."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from shared.models.agency import Agency  # noqa: E402
from src.agencies.agencies_manager import seats_max_for, subscription_is_active  # noqa: E402
from src.core.database import async_session_maker  # noqa: E402


def category(agency: Agency) -> str:
    if agency.plan is None and agency.converted_at is None:
        return "trial/no-plan"
    if agency.billing_mode == "paddle" and agency.billing_status == "canceled":
        return "paddle-dead"
    if agency.converted_at is None or agency.plan is None:
        return "half-converted"
    return f"active-{agency.billing_mode}"


async def main() -> int:
    async with async_session_maker() as db:
        agencies = (await db.execute(select(Agency))).scalars().all()
    counts: dict[str, int] = {}
    failures: list[str] = []
    for agency in agencies:
        cat = category(agency)
        counts[cat] = counts.get(cat, 0) + 1
        expected = None if subscription_is_active(agency) else 3
        got = seats_max_for(agency)
        if got != expected:
            failures.append(f"{agency.slug}: {cat} → seats_max {got} (expected {expected})")
        # The rule, restated independently: an agency WITHOUT plan or WITHOUT
        # conversion date must NEVER be uncapped.
        if (agency.plan is None or agency.converted_at is None) and got is None:
            failures.append(f"{agency.slug}: plan-less agency served unlimited seats")
    for cat in sorted(counts):
        sample = next(a for a in agencies if category(a) == cat)
        print(f"  {cat:16} × {counts[cat]:3} → seats_max {seats_max_for(sample)}")
    if failures:
        print("FAILURES:")
        for line in failures:
            print(f"  !! {line}")
        return 1
    print(f"OK — {len(agencies)} agencies, every row agrees with the rule")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
