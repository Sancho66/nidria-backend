"""Correct the two original French fixture labels, in the sandbox offline only."""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.expat_user import ExpatUser
from src.core.config import get_settings
from src.core.database import async_session_maker, engine
from src.demo_sandbox.guards import assert_isolated_settings


async def repair_labels(db: AsyncSession) -> int:
    agencies = list(await db.scalars(select(Agency).where(Agency.default_language == "fr")))
    changed = 0
    for agency in agencies:
        if not agency.slug.startswith("demo-sandbox-") or not agency.settings.get(
            "sandbox_catalog"
        ):
            continue
        agents = await db.scalars(select(Agent).where(Agent.agency_id == agency.id))
        for agent in agents:
            if (agent.first_name, agent.last_name) == ("Demo", "Equipe"):
                agent.first_name, agent.last_name = "Démo", "Équipe"
                changed += 1
        cases = await db.scalars(select(ClientCase).where(ClientCase.agency_id == agency.id))
        for case in cases:
            if not case.is_demo:
                raise RuntimeError("A sandbox fixture unexpectedly contains a non-demo case")
            client = await db.get(ExpatUser, case.principal_expat_user_id)
            if client and (client.first_name, client.last_name) == ("Client", "Demonstration"):
                client.last_name = "Démonstration"
                changed += 1
            persons = await db.scalars(select(CasePerson).where(CasePerson.case_id == case.id))
            for person in persons:
                if person.full_name == "Client Demonstration":
                    person.full_name = "Client Démonstration"
                    changed += 1
        profiles = await db.scalars(
            select(ClientProfile).where(ClientProfile.agency_id == agency.id)
        )
        for profile in profiles:
            if (profile.first_name, profile.last_name) == ("Client", "Demonstration"):
                profile.last_name = "Démonstration"
                changed += 1
    await db.commit()
    return changed


async def main() -> None:
    assert_isolated_settings(get_settings(), seed=True)
    async with async_session_maker() as db:
        count = await repair_labels(db)
    await engine.dispose()
    print(f"Corrected {count} fictional French labels; codes and journeys unchanged")


if __name__ == "__main__":
    asyncio.run(main())
