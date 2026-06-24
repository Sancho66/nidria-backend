"""One-off migration: promote Alexandre & Eric to the platform-reserved
`superadmin` role (every permission + agency.create), IN PLACE.

Why a script and not just a re-seed: scripts/seed.py is idempotent BY EMAIL
and NEVER migrates an existing agent's role (it only creates missing rows). On
an already-seeded database (dev or prod) the two founders already exist as
`admin`, so a re-seed leaves their role untouched. This script closes that gap:

  1. Upgrades the `superadmin` SYSTEM ROLE to its full permission set
     (seed_system_roles is ADDITIVE — it inserts the now-missing
     role_permission rows; it never deletes). Safe on every deploy.
  2. Flips the two founders' role_id to `superadmin` — SAME agency. They keep
     their home agency and every bit of their data; they simply gain
     agency.create + the full permission set and lose nothing.

Idempotent and transactional: re-running is a no-op. The superadmin role is
platform-reserved — agencies can neither list nor assign it (baseline:
PLATFORM_ROLE_NAMES), so this is the ONLY path that grants it.

Run:  uv run python scripts/migrate_superadmins.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from shared.models import Agent, Role  # noqa: E402
from src.core.database import async_session_maker  # noqa: E402
from src.core.rbac.baseline import seed_system_roles  # noqa: E402

# Nidria's platform operators — the ONLY holders of the superadmin role.
SUPERADMIN_EMAILS: tuple[str, ...] = (
    "alexandre.montilla@gmail.com",
    "mr.schalk.eric@gmail.com",
)


async def migrate() -> None:
    async with async_session_maker() as db:
        # 1. Additive role upgrade — superadmin gains every (non-external)
        #    permission + agency.create. Commits internally; never deletes.
        await seed_system_roles(db)

        # 2. Flip the two founders to the superadmin role (in place).
        superadmin = (
            await db.execute(select(Role).where(Role.is_system, Role.name == "superadmin"))
        ).scalar_one()

        changed: list[str] = []
        for email in SUPERADMIN_EMAILS:
            agent = (
                await db.execute(select(Agent).where(Agent.email == email))
            ).scalar_one_or_none()
            if agent is None:
                print(f"  SKIP {email}: no agent with this email")
                continue
            if agent.role_id == superadmin.id:
                print(f"  OK   {email}: already superadmin")
                continue
            agent.role_id = superadmin.id
            changed.append(email)
        await db.commit()

        for email in changed:
            print(f"  DONE {email}: role → superadmin")

    print("=" * 60)
    print(f"Superadmin migration complete ({len(changed)} agent(s) updated).")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(migrate())
