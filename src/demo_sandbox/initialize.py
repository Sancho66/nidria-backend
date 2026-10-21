"""Offline initialization of the dedicated sandbox, never the serving process."""

import asyncio
import json

from alembic.config import Config
from sqlalchemy import text

from alembic import command
from src.core.config import get_settings
from src.core.database import async_session_maker, engine
from src.demo_sandbox.catalog import load_catalog
from src.demo_sandbox.guards import assert_isolated_settings
from src.demo_sandbox.seed import seed_catalog


async def initialize() -> None:
    async with async_session_maker() as db:
        result = await seed_catalog(db, load_catalog())
        # Production migrations enable deny-all RLS. Grant SELECT only to the
        # dedicated runtime role, on this separate fictional database only.
        tables = await db.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            await db.execute(text(f"GRANT SELECT ON public.{quoted} TO nidria_demo_reader"))
            await db.execute(text(f"DROP POLICY IF EXISTS demo_read_only ON public.{quoted}"))
            await db.execute(
                text(
                    f"CREATE POLICY demo_read_only ON public.{quoted} "
                    "FOR SELECT TO nidria_demo_reader USING (true)"
                )
            )
        await db.execute(text("GRANT USAGE ON SCHEMA public TO nidria_demo_reader"))
        await db.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        await db.commit()
        print(json.dumps(result))
    await engine.dispose()


if __name__ == "__main__":
    assert_isolated_settings(get_settings(), seed=True)
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(initialize())
