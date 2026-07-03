"""Manual 2FA recovery (MVP): purge a user's TOTP enrollment.

Phone lost AND backup codes exhausted/lost is the ONLY case for this
script: there is deliberately NO automatic email reset (an email path
would void the second factor — whoever holds the mailbox could disarm
2FA). Procedure:

  1. Verify the person's identity OUT-OF-BAND (call with the agency /
     Eric — never on the say-so of an email).
  2. Run, in prod:
       fly ssh console -a nidria-api -C \\
         "python scripts/reset_mfa.py agent user@agency.com"
     (first argument: agent | expat)
  3. Tell the user to re-enroll: Settings -> 2FA -> setup + enable.

The purge cascades the backup codes; active sessions are untouched
(the user is simply back to password-only login)."""

import asyncio
import sys

from sqlalchemy import delete, select

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.mfa import MfaTotp
from src.core.database import async_session_maker
from src.core.email import normalize_email


async def main(actor_type: str, email: str) -> int:
    if actor_type not in ("agent", "expat"):
        print("usage: reset_mfa.py <agent|expat> <email>")
        return 2
    model = Agent if actor_type == "agent" else ExpatUser
    async with async_session_maker() as db:
        actor = (
            await db.execute(select(model).where(model.email == normalize_email(email)))
        ).scalar_one_or_none()
        if actor is None:
            print(f"NOT FOUND: no {actor_type} with email {email}")
            return 1
        result = await db.execute(
            delete(MfaTotp).where(MfaTotp.actor_type == actor_type, MfaTotp.actor_id == actor.id)
        )
        await db.commit()
        if result.rowcount:
            print(f"OK: 2FA purged for {actor_type} {email} — they can re-enroll now.")
        else:
            print(f"NO-OP: no 2FA enrollment for {actor_type} {email}.")
        return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: reset_mfa.py <agent|expat> <email>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2])))
