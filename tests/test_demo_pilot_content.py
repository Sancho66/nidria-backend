"""Regression coverage for the five-pilot review, using only fictional fixtures."""

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.email_campaign import EmailCampaign
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from shared.models.rbac import Role
from shared.models.step_requirement import StepRequirement
from src.admin.demo_schema import AgencyDemoBatchRequest, AgencyDemoInput
from src.core.rbac.baseline import seed_rbac_baseline
from src.demo_sandbox.agency_control import AgencyDemoControlManager
from src.demo_sandbox.agency_draft import STEP_TITLES, agency_record
from src.demo_sandbox.agency_manager import AgencyDemoManager
from src.demo_sandbox.catalog import load_catalog
from src.demo_sandbox.people import fictional_people
from src.demo_sandbox.schema import DemoSessionRequest
from tests.test_agency_demo import pilot


@pytest.mark.parametrize("language", ["en", "fr"])
@pytest.mark.parametrize("profession", ["accountant", "lawyer", "formation", "relocation"])
def test_short_fallback_titles_preserve_full_descriptions(language: str, profession: str) -> None:
    data = pilot().model_dump()
    data.update(language=language, profession=profession, steps=["A complete sentence " * 20] * 6)
    record = agency_record(AgencyDemoInput.model_validate(data), "a" * 32, load_catalog())
    steps = record.journey_import["parcours"]["etapes"]
    assert [step["nom"][language] for step in steps] == STEP_TITLES[language][profession]
    assert all(2 <= len(step["nom"][language].split()) <= 5 for step in steps)
    assert all("…" not in step["nom"][language] for step in steps)
    assert [step["informations_fournies"]["note"][language] for step in steps] == [
        sentence.strip() for sentence in data["steps"]
    ]


def test_optional_titles_journey_and_documents_are_authoritative() -> None:
    data = pilot().model_dump()
    data.update(
        profession="lawyer",
        country="US",
        journey_name="Permanent residence application",
        step_titles=[
            "Engagement letter",
            "Document collection",
            "",
            "I-485 preparation",
            "Signature and filing",
            "USCIS follow-up",
        ],
        documents=[
            "Certified court dispositions",
            "Passports",
            "I-94 records",
            "Birth certificates",
        ],
    )
    row = AgencyDemoInput.model_validate(data)
    record = agency_record(row, "a" * 32, load_catalog())
    steps = record.journey_import["parcours"]["etapes"]
    assert record.promised_journey == data["journey_name"]
    assert [step["nom"]["en"] for step in steps] == [
        title or STEP_TITLES["en"]["lawyer"][i] for i, title in enumerate(data["step_titles"])
    ]
    assert {doc["label"] for doc in record.state["documents"]} == set(data["documents"])
    assert {doc["status"] for doc in record.state["documents"]} == {"validated", "requested"}
    assert "Employment letter" not in str(record.model_dump())
    for override in (
        {"step_titles": ["Too many words for a step title"] * 6},
        {"step_titles": ["One"] * 6},
        {"step_titles": ["Only two"]},
        {"documents": ["Passport", "passport"]},
        {"documents": [" "]},
    ):
        with pytest.raises(ValidationError):
            AgencyDemoInput.model_validate({**data, **override})


@pytest.mark.asyncio
async def test_country_identities_roles_and_no_shared_clients(db_session: AsyncSession) -> None:
    catalog = load_catalog()
    await seed_rbac_baseline(db_session)
    await db_session.commit()
    data = pilot().model_dump()
    data.update(
        company_name="Fictional Pilot",
        country="US",
        profession="lawyer",
        documents=[
            "Certified court dispositions",
            "Passports",
            "I-94 records",
            "Birth certificates",
        ],
        journey_name="Permanent residence application",
    )
    inputs = [AgencyDemoInput.model_validate(data)]
    # Same company/name pools in two independent demos must never share a login.
    for n in (1, 2):
        inputs.append(
            AgencyDemoInput.model_validate(
                {**data, "country": "AU", "email": f"pilot{n}@example.com"}
            )
        )
    result = await AgencyDemoControlManager(db_session, catalog).create_batch(
        uuid.uuid4(), AgencyDemoBatchRequest(request_id=uuid.uuid4(), items=inputs)
    )
    all_client_ids = set()
    all_emails = set()
    for row, entry in zip(inputs, result.items, strict=True):
        agency = (
            await db_session.scalars(
                select(Agency).where(Agency.slug == f"demo-sandbox-{entry.code}")
            )
        ).one()
        people = fictional_people(row)
        members = list(
            (
                await db_session.execute(
                    select(Agent, Role).join(Role).where(Agent.agency_id == agency.id)
                )
            ).all()
        )
        assert {role.name for agent, role in members if not agent.is_external} == {
            "admin",
            "case_manager",
            "member",
        }
        assert {(agent.first_name, agent.last_name) for agent, _ in members} == set(people.team)
        cases = list(
            await db_session.scalars(select(ClientCase).where(ClientCase.agency_id == agency.id))
        )
        assert len(cases) == 10 and all(case.dest_country == row.country for case in cases)
        clients = list(
            await db_session.scalars(
                select(ExpatUser).where(
                    ExpatUser.id.in_([case.principal_expat_user_id for case in cases])
                )
            )
        )
        assert {(client.first_name, client.last_name) for client in clients} == set(people.clients)
        assert not all_client_ids.intersection(client.id for client in clients)
        all_client_ids.update(client.id for client in clients)
        emails = {client.email for client in clients} | {agent.email for agent, _ in members}
        assert len(emails) == 14 and not emails.intersection(all_emails)
        assert all(email.endswith(".example") and entry.code not in email for email in emails)
        all_emails.update(emails)
        journey = (
            await db_session.scalars(
                select(JourneyTemplate).where(JourneyTemplate.agency_id == agency.id)
            )
        ).one()
        assert journey.country == row.country and journey.name == row.journey_name
        references = set(
            await db_session.scalars(
                select(StepRequirement.reference)
                .join(JourneyTemplateStep)
                .where(
                    JourneyTemplateStep.template_id == journey.id,
                    StepRequirement.kind == "document",
                )
            )
        )
        assert references == {*row.documents, "Example signature"}
        campaigns = list(
            await db_session.scalars(
                select(EmailCampaign).where(EmailCampaign.agency_id == agency.id)
            )
        )
        assert campaigns == []
        session = await AgencyDemoManager(db_session).open_session(
            DemoSessionRequest(code=entry.code)
        )
        assert session is not None and session.country == row.country
    assert fictional_people(inputs[0]).clients != fictional_people(inputs[1]).clients
