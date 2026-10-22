"""Grant the separate sandbox writer only the rights needed to create fixtures."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.database import async_session_maker, engine
from src.demo_sandbox.guards import assert_isolated_settings

WRITABLE_TABLES = frozenset(
    {
        "agency",
        "agent",
        "activity_log",
        "agency_usage_milestone",
        "usage_event",
        "client_case",
        "client_profile",
        "case_person",
        "case_step_progress",
        "case_step_requirement",
        "case_step_participant",
        "custom_field_definition",
        "expat_user",
        "journey_template",
        "journey_template_step",
        "journey_step_participant",
        "journey_template_field",
        "step_prerequisite",
        "step_requirement",
        "document",
        "reminder",
        "signature_request",
        "signature_signer",
    }
)


async def grant_management(db: AsyncSession) -> None:
    role = (
        await db.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication "
                "FROM pg_roles WHERE rolname='nidria_demo_writer'"
            )
        )
    ).one_or_none()
    if role is None or any(role):
        raise RuntimeError("Create a dedicated non-administrative writer role first")
    tables = set(
        await db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
    )
    if not WRITABLE_TABLES.issubset(tables):
        raise RuntimeError("The sandbox schema is not current")
    await db.execute(text("GRANT USAGE ON SCHEMA public TO nidria_demo_writer"))
    for table in sorted(tables):
        quoted = '"' + table.replace('"', '""') + '"'
        await db.execute(text(f"REVOKE ALL ON public.{quoted} FROM nidria_demo_writer"))
        await db.execute(text(f"GRANT SELECT ON public.{quoted} TO nidria_demo_writer"))
        await db.execute(text(f"DROP POLICY IF EXISTS demo_management ON public.{quoted}"))
        if table in WRITABLE_TABLES:
            await db.execute(text(f"GRANT INSERT, UPDATE ON public.{quoted} TO nidria_demo_writer"))
            policy = "FOR ALL TO nidria_demo_writer USING (true) WITH CHECK (true)"
        else:
            policy = "FOR SELECT TO nidria_demo_writer USING (true)"
        await db.execute(text(f"CREATE POLICY demo_management ON public.{quoted} {policy}"))
    await db.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC, nidria_demo_writer"))
    await db.commit()


async def main() -> None:
    assert_isolated_settings(get_settings(), seed=True)
    async with async_session_maker() as db:
        await grant_management(db)
    await engine.dispose()
    print("Isolated management privileges installed; public reader rights unchanged")


if __name__ == "__main__":
    asyncio.run(main())
